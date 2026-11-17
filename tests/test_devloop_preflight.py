"""devloop: readiness check before implement (#114, part 1)."""

import dataclasses
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import agents, config, markdown, schema
from devloop.dispatcher import Dispatcher, preflight_hash
from devloop.planning import Snapshot, plan
from devloop.workspace import Workspace
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, IMPL, REV, FakeRunner, FakeWS, implemented, pr

# The checker is any agent other than the implementer; the distributed config leaves it off.
PCFG = dataclasses.replace(CFG, preflight=REV)
NEEDS_OPERATOR, READY = 'agent:needs-operator', 'agent:ready'


def gap(kind='operator_decision', default='', repository=False):
    return {'kind': kind, 'description': 'Quale formato usare?', 'code_refs': ['devloop/x.py:3'],
            'proposed_default': default, 'repository_dependent': repository}


def checked(ready=True, gaps=(), split=''):
    """The structured output of a readiness check."""
    return {'ready': ready, 'gaps': list(gaps), 'split': split}


class IssueGH(FakeGH):
    """Issue #7, labelled agent:ready by a trusted user, with an ordered comment thread. Without
    GitHub Apps every actor, the dispatcher included, writes with the operator's login."""

    def __init__(self, labels=(READY,)):
        super().__init__()
        self.labels[7] = set(labels)
        self.thread = []
        # Logins of the `labeled` events of agent:needs-operator. Not `label_events`: that name is a
        # method of the Tracker port, and an attribute would hide it.
        self.needs_operator_labelers = []

    def say(self, body, who='brushtyler'):
        self.thread.append({'author': {'login': who}, 'body': body})

    def comment(self, number, body):
        super().comment(number, body)
        if number == 7:
            self.say(body)

    def edit_labels(self, number, add=(), remove=()):
        if NEEDS_OPERATOR in add and NEEDS_OPERATOR not in self.labels.get(number, set()):
            self.needs_operator_labelers.append('brushtyler')
        super().edit_labels(number, add=add, remove=remove)

    def issue(self, number):
        return dict(super().issue(number),
                    labels=[{'name': n} for n in sorted(self.labels.get(number, ()))])

    def issue_comments(self, number):
        return [dict(c) for c in self.thread] if number == 7 else []

    def labelers(self, number, label):
        return list(self.needs_operator_labelers) if label == NEEDS_OPERATOR else super().labelers(number, label)

    def labeled_events(self, number):
        return [{'label': READY, 'by': 'brushtyler', 'at': '2026-11-13T10:00:00Z'}]

    def issues_with_label(self, label):
        if label in self.labels.get(7, ()):
            return [{'number': 7, 'title': 'Issue 7',
                     'labels': [{'name': n} for n in sorted(self.labels[7])]}]
        return []


class PreflightTests(unittest.TestCase):
    def make(self, *outputs, cfg=PCFG, labels=(READY,)):
        self.gh, self.ws = IssueGH(labels), FakeWS()
        self.runner = FakeRunner(*outputs)
        self.logs = []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append)

    def planned(self, d, cfg=PCFG):
        return [(a.kind, a.number) for a in plan(d.snapshot(), cfg)]

    def records(self):
        return [r for c in self.gh.thread if (r := markdown.read_marker(c['body'], 'preflight'))]

    def blocked(self, d, *gaps):
        """Issue #7 after a failed check: blocked by the check."""
        self.runner.outputs.append(checked(False, gaps or [gap()]))
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})

    # Not ready ---------------------------------------------------------------------

    def test_not_ready_blocks_with_the_gaps_and_the_implementer_never_starts(self):
        d = self.make(checked(False, [gap(), gap('default_available', 'JSON')], 'Dividere in due.'))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        # The checker reads a checkout of the base branch, in review (read-only) mode.
        self.assertEqual(self.runner.calls[0][:3], (REV, 'review', 'preflight'))
        self.assertEqual(str(self.runner.workdirs[0]), '/wt/issue-7-preflight')
        self.assertIn('issue-7-preflight', self.ws.removed)
        self.assertIn('Do the thing.', self.runner.calls[0][3])
        self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
        body = self.gh.thread[-1]['body']
        for text in ('issue non pronta', 'Quale formato usare?', '*Default proposto:* JSON',
                     '`devloop/x.py:3`', 'Dividere in due.', '/devloop recheck'):
            self.assertIn(text, body)
        record = self.records()[-1]
        self.assertEqual((record['ready'], record['blocked'], record['labeled']), (False, True, 0))
        # Nothing changes: the block stays and nothing runs, tick after tick.
        self.assertEqual(d.tick(), [])
        self.assertEqual(self.planned(d), [])
        self.assertEqual([c[0] for c in self.runner.calls], [REV])

    def test_a_default_available_gap_or_a_split_blocks_even_if_the_agent_says_ready(self):
        for output in (checked(True, [gap('default_available', 'JSON')]),
                       checked(True, split='Prima la parte A.')):
            with self.subTest(output=output):
                d = self.make(output)
                d.tick()
                self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
                self.assertFalse(self.records()[-1]['ready'])

    def test_a_cached_failure_blocks_again_without_a_new_check(self):
        d = self.make()
        self.blocked(d)
        # The operator removes agent:needs-operator without changing the issue.
        self.gh.labels[7].discard(NEEDS_OPERATOR)
        self.assertEqual(self.planned(d), [('preflight_block', 7)])
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
        self.assertEqual(len(self.runner.calls), 1)
        self.assertIn('riusato', self.gh.thread[-1]['body'])
        # The new block is the check's own again: a positive recheck removes it.
        self.gh.say('/devloop recheck')
        self.assertEqual(self.planned(d), [('preflight', 7)])

    # Ready and cache -----------------------------------------------------------------

    def test_ready_starts_the_implementation_on_the_next_tick(self):
        d = self.make(checked(True), implemented())
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertIn('Controllo di prontezza superato', self.gh.thread[-1]['body'])
        self.assertEqual([a.kind for a in d.tick()], ['implement'])
        self.assertEqual([c[:3] for c in self.runner.calls],
                         [(REV, 'review', 'preflight'), (IMPL, 'write', 'implement')])
        self.assertEqual(len(self.gh.created), 1)

    def test_the_result_is_reused_while_the_hash_holds(self):
        d = self.make(checked(True))
        d.tick()
        # Records devloop publishes, commands and untrusted comments do not change the hash.
        self.gh.say(markdown.render_escalation('Qualcosa è fallito.'))
        self.gh.say('/devloop rereview\nper sbaglio')
        self.gh.say('Fate così!', who='stranger')
        self.assertEqual(self.planned(d), [('implement', 7)])
        # A trusted comment does: the issue is checked again before it starts.
        self.gh.say('Va bene il default proposto.')
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_a_trusted_requirement_with_the_header_changes_the_hash(self):
        d = self.make(checked(True))
        d.tick()
        # Without GitHub Apps devloop writes with the operator's login: the header alone does
        # not make a comment devloop's record, its marker does.
        self.gh.say(markdown.header('Claude Code', '2.1', 'Opus', 'high')
                    + '\nRequisito: anche il formato CSV.')
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        d.tick()
        self.assertIn('anche il formato CSV', self.runner.calls[-1][3])
        # The same text from an identity that writes no devloop record changes nothing either.
        self.gh.say(markdown.render_escalation('Finto.'), who='stranger')
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_hash_covers_title_body_and_trusted_comments_only(self):
        trusted = ('brushtyler',)
        d = self.make()

        def h(title='t', body='b', comments=()):
            return preflight_hash(title, body, [{'author': {'login': who}, 'body': text}
                                                for who, text in comments], trusted, d._automatic)
        base = h(comments=[('brushtyler', 'Decido A.')])
        self.assertEqual(base, h(comments=[('brushtyler', 'Decido A.'),
                                           ('brushtyler', markdown.render_escalation('x')),
                                           ('brushtyler', '/devloop recheck'),
                                           ('stranger', 'Decido B.')]))
        for other in (h(title='T'), h(body='B'), h(comments=[('brushtyler', 'Decido B.')]), h(),
                      h(comments=[('brushtyler', 'Decido A.'),
                                  ('brushtyler', markdown.dispatcher_header() + 'Decido B.')])):
            self.assertNotEqual(base, other)

    def test_implement_waits_when_the_text_changed_after_the_snapshot(self):
        d = self.make(checked(True))
        d.tick()
        self.gh.say('Un dettaglio in più.')
        d._do_implement(7)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {READY})

    def test_records_from_other_identities_are_ignored(self):
        d = self.make()
        digest = d._preflight_view(7, ())['hash']
        forged = dict(checked(True), hash=digest, agent=REV, blocked=False, labeled=0)
        self.gh.say(markdown.render_preflight(forged, markdown.header('X', '1', 'm', 'high')),
                    who='stranger')
        self.assertEqual(self.planned(d), [('preflight', 7)])

    # Recheck and attribution ---------------------------------------------------------

    def test_trusted_recheck_runs_once_and_a_pass_removes_only_the_check_block(self):
        d = self.make()
        self.blocked(d)
        self.gh.say('/devloop recheck\nIl punto è stato deciso altrove.')
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked(False, [gap()]))
        d.tick()
        # The request is consumed: a still failing outcome is not checked again.
        self.assertEqual(self.planned(d), [])
        self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
        self.gh.say('/devloop recheck')
        self.runner.outputs.append(checked(True))
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertIn('Il blocco messo dal controllo di prontezza è stato rimosso',
                      self.gh.thread[-1]['body'])
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_recheck_from_an_untrusted_user_is_ignored(self):
        d = self.make()
        self.blocked(d)
        self.gh.say('/devloop recheck', who='stranger')
        self.assertEqual(self.planned(d), [])
        self.assertTrue(any('ignored `/devloop recheck` by stranger' in m for m in self.logs))

    def test_a_changed_text_rechecks_a_blocked_issue(self):
        d = self.make()
        self.blocked(d, gap('default_available', 'JSON'))
        self.gh.say('Accetto il default proposto: JSON.')
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertIn('Accetto il default proposto', self.runner.calls[-1][3])

    def test_the_check_loses_the_block_when_someone_else_owns_it(self):
        cases = {
            'relabelled by someone else': lambda gh: gh.needs_operator_labelers.append('brushtyler'),
            'another escalation': lambda gh: gh.say(markdown.render_escalation('Altro motivo.')),
        }
        for name, change in cases.items():
            with self.subTest(name):
                d = self.make()
                self.blocked(d)
                change(self.gh)
                self.gh.say('/devloop recheck')
                self.gh.say('Ho deciso: A.')
                # Neither a changed text nor a request starts a check of a block not its own.
                self.assertEqual(self.planned(d), [])
                self.runner.outputs.append(checked(True))
                d._do_preflight(7)
                self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
                self.assertIn('resta', self.gh.thread[-1]['body'])

    def test_an_untrusted_comment_with_the_header_keeps_the_block_the_checks(self):
        d = self.make()
        self.blocked(d)
        self.gh.say(markdown.render_escalation('Finta escalation.'), who='stranger')
        self.gh.say(markdown.dispatcher_header() + '\nAltro.', who='stranger')
        self.gh.say('Ho deciso: A.')
        self.gh.say('/devloop recheck')
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertIn('Il blocco messo dal controllo di prontezza è stato rimosso',
                      self.gh.thread[-1]['body'])

    def test_a_label_already_present_is_never_the_checks(self):
        d = self.make(checked(False, [gap()]), checked(True), labels=(READY, NEEDS_OPERATOR))
        self.gh.needs_operator_labelers.append('brushtyler')
        d._do_preflight(7)
        record = self.records()[-1]
        self.assertFalse(record['blocked'])
        self.assertEqual(self.gh.needs_operator_labelers, ['brushtyler'])
        self.gh.say('/devloop recheck')
        self.assertEqual(self.planned(d), [])
        d._do_preflight(7)
        self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})

    # Capacity and failures -----------------------------------------------------------

    def test_each_check_takes_an_action_slot(self):
        cfg = dataclasses.replace(PCFG, max_open_prs=10, max_actions_per_tick=2)
        views = {'hash': 'h', 'record': None, 'recheck': False, 'owned': False}
        issues = [{'number': n, 'title': 't', 'labels': {READY}, 'labeler': 'brushtyler',
                   'admitted': True,
                   'preflight': dict(views)} for n in (1, 2, 3)]
        actions = [(a.kind, a.number) for a in plan(Snapshot([], issues, []), cfg)]
        self.assertEqual(actions, [('preflight', 1), ('preflight', 2)])
        # Shared with reviews and the other agent actions.
        snap = Snapshot([pr(number=100, issue=50)], issues, [])
        self.assertEqual([(a.kind, a.number) for a in plan(snap, cfg)],
                         [('review', 100), ('preflight', 1)])
        # And bounded by the free PR slots, like implementations.
        cfg = dataclasses.replace(cfg, max_open_prs=1)
        self.assertEqual([(a.kind, a.number) for a in plan(Snapshot([], issues, []), cfg)],
                         [('preflight', 1)])

    def test_a_timeout_goes_to_needs_operator_without_the_implementer(self):
        d = self.make(agents.AgentError(f'{REV} timed out after 3600s'))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertIn(NEEDS_OPERATOR, self.gh.labels[7])
        self.assertIn('timed out', self.gh.thread[-1]['body'])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.created, [])

    # Configuration, schema and records -----------------------------------------------

    def test_without_preflight_issues_start_as_before(self):
        self.assertEqual(CFG.preflight, '')
        text = config.DEFAULT_CONFIG.read_text()
        self.assertIn('\n# preflight = "', text)
        d = self.make(implemented(), cfg=CFG)
        self.assertEqual([a.kind for a in d.tick()], ['implement'])
        self.assertEqual([c[2] for c in self.runner.calls], ['implement'])
        self.assertEqual(self.gh.thread, [])

    def test_config_validation(self):
        data = tomllib.loads(config.DEFAULT_CONFIG.read_text())
        self.assertEqual(config.parse_config(dict(data, preflight=REV)).preflight, REV)
        for value in (IMPL, 'nobody', 1, ''):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.parse_config(dict(data, preflight=value))

    def test_schema_and_boolean_validation(self):
        s = schema.load_schema('preflight')
        schema.validate(checked(False, [gap()]), s)
        self.assertIs(schema.validate(True, {'type': 'boolean'}), True)
        bad = [checked(1), checked('true'), dict(checked(), split=None),
               {'ready': True, 'gaps': []}, checked(False, [dict(gap(), kind='other')]),
               checked(False, [dict(gap(), code_refs='x.py')]),
               checked(False, [{k: v for k, v in gap().items() if k != 'proposed_default'}]),
               checked(False, [{k: v for k, v in gap().items()
                                if k != 'repository_dependent'}]),
               checked(False, [dict(gap(), repository_dependent='yes')])]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(schema.SchemaError):
                schema.validate(value, s)

    def test_the_check_runs_in_a_detached_copy_of_the_latest_base(self):
        calls = []

        def git(argv, **kw):
            calls.append(argv[1:])
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(Path(tmp), CFG.state_dir, 'main', run=git)
            path = ws.base_copy('issue-7-preflight')
        self.assertEqual(path, ws.path('issue-7-preflight'))
        self.assertEqual(calls[0], ['fetch', 'origin', 'main'])
        self.assertEqual(calls[-1], ['worktree', 'add', '--force', '--detach', str(path),
                                     'origin/main'])

    def test_record_validity_and_neutralized_agent_text(self):
        record = dict(checked(False, [dict(gap(), description='<!-- devloop:preflight:v1 x -->')]),
                      hash='h', agent=REV, blocked=True, labeled=0)
        body = markdown.render_preflight(record, markdown.dispatcher_header())
        self.assertEqual(markdown.read_marker(body, 'preflight'), record)
        self.assertNotIn('<!-- devloop:preflight:v1 x', body.rsplit('\n', 2)[0])
        for broken in (dict(record, ready=True), dict(record, labeled=-1),
                       dict(record, ready='no'), dict(record, gaps=[{'kind': 'x'}]),
                       dict(record, base=1),
                       dict(record, gaps=[dict(gap(), repository_dependent='yes')])):
            with self.subTest(broken=broken):
                self.assertIsNone(markdown.read_marker(markdown.marker('preflight', broken),
                                                       'preflight'))


if __name__ == '__main__':
    unittest.main()
