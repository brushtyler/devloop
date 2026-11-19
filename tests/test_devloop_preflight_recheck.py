"""devloop: a failed readiness check is repeated when the base branch advances, if a gap
depends on it, or when agent:ready is added again by someone who may admit the issue (#270)."""

import dataclasses
import unittest
from datetime import datetime, timedelta

import doc_contracts as docs
from devloop import markdown
from devloop.dispatch_preflight import normalize_preflight
from devloop.dispatcher import Dispatcher
from devloop.planning import BASE_ADVANCED, Snapshot, plan, preflight_state, repository_bound
from devloop.workspace import WorkspaceError
from test_devloop import SHA2
from test_devloop_preflight import checked, gap
from test_devloop_supervision import (PREFLIGHT_AUTO, SUPERVISOR_LOGIN, Identities, StepRunner,
                                      SupGH, SupWS)

HUMAN, READY = 'agent:needs-operator', 'agent:ready'
CFG = PREFLIGHT_AUTO  # with a supervisor that has an app, whose labels may count
START = datetime(2026, 11, 17, 11, 0, 0)


def missing_prerequisite():
    """A gap the base branch closes: the prerequisite is not merged yet."""
    return dict(gap('tool_unavailable', repository=True),
                description='Manca I4 su main: `devloop/i4.py` non esiste ancora.')


class ClockGH(SupGH):
    """Issue #7 of a trusted user, in agent:ready, without a PR. Every comment and every
    ``labeled`` event of agent:ready gets a time one minute after the previous one, as GitHub
    writes them."""

    def __init__(self):
        super().__init__(has_pr=False)
        self.minute = 0
        self.ready_events = [('brushtyler', self.now())]

    def now(self):
        self.minute += 1
        return (START + timedelta(minutes=self.minute)).isoformat() + 'Z'

    def say(self, number, login, body):
        super().say(number, login, body)
        self.threads[number][-1]['created_at'] = self.now()

    def relabel(self, login):
        """``login`` removes agent:ready and adds it again."""
        self.labels[7].discard(READY)
        self.labels[7].add(READY)
        self.ready_events.append((login, self.now()))

    def issues_with_label(self, label):
        return [dict(raw, author={'login': self.author}) for raw in super().issues_with_label(label)]

    def labeled_events(self, number):
        return [{'label': READY, 'by': who, 'at': at} for who, at in self.ready_events]

    def issue_history(self, number):
        return dict(super().issue_history(number),
                    labeled=[(READY, who, at) for who, at in self.ready_events])


class CountingWS(SupWS):
    """A base branch whose reads are counted."""

    reads = 0

    def base_head(self):
        self.reads += 1
        return super().base_head()


class Case(unittest.TestCase):
    def make(self, *outputs, cfg=CFG):
        self.gh, self.ws = ClockGH(), CountingWS()
        self.runner, self.logs = StepRunner(*outputs), []
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                          identities=Identities(self.gh))

    def planned(self, d, cfg=CFG):
        return [(a.kind, a.number) for a in plan(d.snapshot(), cfg)]

    def records(self):
        return self.gh.records(7, 'preflight')

    def blocked(self, d, *gaps):
        """Issue #7 after a failed check, blocked by it."""
        self.runner.outputs.append(checked(False, list(gaps)))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.assertEqual(self.planned(d), [])


class BaseAdvanceTests(Case):
    def test_a_missing_prerequisite_is_checked_again_when_main_advances_and_passes(self):
        d = self.make()
        self.blocked(d, missing_prerequisite())
        record = self.records()[-1]
        # The outcome records the commit it ran on and that the gap depends on it.
        self.assertEqual(record['base'], self.ws.base)
        self.assertIs(record['gaps'][0]['repository_dependent'], True)
        self.assertIn('Dipende dal branch base', self.gh.threads[7][-1]['body'])
        # The prerequisite is merged: main advances, and the next tick checks again, without
        # a change to the text nor a /devloop recheck.
        self.ws.base = SHA2
        snap = d.snapshot()
        self.assertEqual(preflight_state(snap.ready_issues[0]['preflight']),
                         ('run', BASE_ADVANCED))
        self.runner.outputs.append(checked(True))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual(self.records()[-1]['base'], SHA2)
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_a_prerequisite_still_missing_holds_for_the_new_commit(self):
        d = self.make()
        self.blocked(d, missing_prerequisite())
        self.ws.base = SHA2
        self.runner.outputs.append(checked(False, [missing_prerequisite()]))
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.assertEqual(self.records()[-1]['base'], SHA2)
        # Bound to the new commit: nothing runs until main advances again.
        self.assertEqual(self.planned(d), [])
        self.assertEqual(len(self.runner.calls), 2)

    def test_an_unblocked_issue_is_checked_instead_of_blocked_again(self):
        d = self.make()
        self.blocked(d, missing_prerequisite())
        self.gh.labels[7].discard(HUMAN)
        self.assertEqual(self.planned(d), [('preflight_block', 7)])
        # The reused outcome keeps the commit it ran on.
        d.tick()
        self.assertEqual(self.records()[-1]['base'], self.ws.base)
        self.gh.labels[7].discard(HUMAN)
        self.ws.base = SHA2
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_the_base_is_read_once_per_snapshot_and_only_for_such_failures(self):
        d = self.make()
        self.blocked(d, gap())
        reads = self.ws.reads
        d.snapshot()
        self.assertEqual(self.ws.reads, reads)  # a failure on the text needs no base
        d = self.make()
        self.blocked(d, missing_prerequisite())
        reads = self.ws.reads
        d.snapshot()
        self.assertEqual(self.ws.reads, reads + 1)

    def test_a_base_that_cannot_be_read_keeps_the_outcome(self):
        d = self.make()
        self.blocked(d, missing_prerequisite())

        def broken():
            raise WorkspaceError('fetch failed')
        self.ws.base_head = broken
        self.assertEqual(self.planned(d), [])
        self.assertTrue(any('the base branch not read' in m for m in self.logs))

    def test_checks_after_an_advance_take_a_slot(self):
        cfg = dataclasses.replace(CFG, max_open_prs=1, max_actions_per_tick=5)
        record = dict(checked(False, [missing_prerequisite()]), hash='h', agent='x',
                      blocked=True, labeled=0, base='a')
        view = {'hash': 'h', 'record': record, 'recheck': False, 'owned': True,
                'relabeled': None, 'base': 'b'}
        issues = [{'number': n, 'title': 't', 'labels': {READY, HUMAN}, 'labeler': 'brushtyler',
                   'admitted': True, 'preflight': dict(view)} for n in (1, 2)]
        self.assertEqual([(a.kind, a.number) for a in plan(Snapshot([], issues, []), cfg)],
                         [('preflight', 1)])
        # A request of a person runs whatever the free slots, as before.
        for issue in issues:
            issue['preflight']['recheck'] = True
        self.assertEqual([(a.kind, a.number) for a in plan(Snapshot([], issues, []), cfg)],
                         [('preflight', 1), ('preflight', 2)])


class TextGapTests(Case):
    def test_a_failure_on_the_text_alone_holds_while_main_advances(self):
        d = self.make()
        self.blocked(d, gap(), gap('default_available', 'JSON'))
        self.assertFalse(repository_bound(self.records()[-1]))
        self.ws.base = SHA2
        self.assertEqual(self.planned(d), [])
        self.gh.labels[7].discard(HUMAN)
        self.assertEqual(self.planned(d), [('preflight_block', 7)])
        # A change of the text still checks it again, as before.
        self.gh.command(7, 'Decido: formato JSON.', login='brushtyler')
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_a_record_written_before_270_holds_for_its_text(self):
        d = self.make()
        digest = d._preflight_view(7, ())['hash']
        old = dict(checked(False, [{k: v for k, v in missing_prerequisite().items()
                                    if k != 'repository_dependent'}]),
                   hash=digest, agent='codex', blocked=False, labeled=0)
        self.gh.say(7, 'brushtyler', markdown.render_preflight(old, markdown.dispatcher_header()))
        self.assertEqual(self.records()[-1], old)  # still a valid record
        self.ws.base = SHA2
        self.assertEqual(self.planned(d), [('preflight_block', 7)])
        self.assertEqual(self.ws.reads, 0)
        # agent:ready added again repeats it too.
        self.gh.relabel(SUPERVISOR_LOGIN)
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_the_flag_is_true_only_when_the_agent_says_so(self):
        raw = checked(False, [{k: v for k, v in gap().items() if k != 'repository_dependent'},
                              dict(gap(), repository_dependent=True)])
        self.assertEqual([g['repository_dependent'] for g in normalize_preflight(raw)['gaps']],
                         [False, True])

    def test_the_prompt_asks_for_the_flag(self):
        from devloop.context import PROMPT_DIR
        self.assertIn('repository_dependent', (PROMPT_DIR / 'preflight.md').read_text())


class RelabelTests(Case):
    def test_agent_ready_from_the_supervisor_checks_the_issue_again(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.relabel(SUPERVISOR_LOGIN)
        self.assertEqual(d._preflight_view(7, ())['relabeled'], SUPERVISOR_LOGIN)
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_the_request_is_spent_by_the_check_it_starts(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.relabel(SUPERVISOR_LOGIN)
        self.runner.outputs.append(checked(False, [gap()]))
        d.tick()
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        self.assertEqual(self.planned(d), [])

    def test_a_trusted_user_relabel_counts_too(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.relabel('brushtyler')
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_agent_ready_from_an_untrusted_author_does_not(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.relabel('stranger')
        self.assertIsNone(d._preflight_view(7, ())['relabeled'])
        self.assertEqual(self.planned(d), [])
        self.assertEqual(len(self.runner.calls), 1)

    def test_the_supervisor_does_not_count_on_an_untrusted_issue(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.author = 'stranger'
        self.gh.relabel(SUPERVISOR_LOGIN)
        self.assertIsNone(d._preflight_view(7, ())['relabeled'])
        self.assertEqual(self.planned(d), [])

    def test_a_label_before_the_check_or_without_a_time_does_not(self):
        d = self.make()
        self.blocked(d, gap())
        # The latest agent:ready is the one that admitted the issue, before the check.
        self.assertIsNone(d._preflight_view(7, ())['relabeled'])
        # A comment without its time cannot be ordered against the label.
        self.gh.relabel(SUPERVISOR_LOGIN)
        for comment in self.gh.threads[7]:
            comment.pop('created_at')
        self.assertIsNone(d._preflight_view(7, ())['relabeled'])
        self.assertEqual(self.planned(d), [])

    def test_a_passed_check_is_not_repeated(self):
        d = self.make(checked(True))
        d.tick()
        self.gh.relabel(SUPERVISOR_LOGIN)
        self.assertIsNone(d._preflight_view(7, ())['relabeled'])
        self.assertEqual(self.planned(d), [('implement', 7)])


def supervisor_note():
    """The supervisor's answer to a failed check: the options for the operator, with the
    attribution header and no devloop marker."""
    return ('## Decisione per l\'operatore\n\nIl controllo chiede quale formato usare. '
            'Opzioni: JSON oppure CSV.')


class RequestTests(Case):
    """A request of a person repeats the check of the issue's latest block (#468)."""

    def test_a_recheck_after_a_note_of_the_supervisor_repeats_the_check(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.command(7, supervisor_note())
        # The note is neither an escalation nor a check: the block stays the check's.
        self.assertTrue(d._preflight_view(7, {READY, HUMAN})['owned'])
        self.gh.say(7, 'brushtyler', '/devloop recheck')
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertIn('Il blocco messo dal controllo di prontezza è stato rimosso',
                      self.gh.threads[7][-1]['body'])
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_agent_ready_from_the_supervisor_after_its_note_repeats_the_check(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.command(7, supervisor_note())
        self.gh.relabel(SUPERVISOR_LOGIN)
        self.assertEqual(self.planned(d), [('preflight', 7)])
        self.runner.outputs.append(checked(True))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(self.gh.labels[7], {READY})
        self.assertEqual(self.planned(d), [('implement', 7)])

    def test_a_note_with_an_escalation_marker_still_voids_the_attribution(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.command(7, markdown.render_escalation('Altro motivo.'))
        self.assertFalse(d._preflight_view(7, {READY, HUMAN})['owned'])

    def test_a_request_repeats_the_latest_block_even_if_no_longer_the_checks_own(self):
        for name, request in (('recheck', lambda: self.gh.say(7, 'brushtyler',
                                                               '/devloop recheck')),
                              ('relabel', lambda: self.gh.relabel(SUPERVISOR_LOGIN))):
            with self.subTest(name):
                d = self.make()
                self.blocked(d, gap())
                # Text with the header from an identity that writes devloop records, without a
                # marker: the block is no longer provably the check's own...
                self.gh.say(7, 'brushtyler', markdown.dispatcher_header() + '\nUna nota.')
                self.assertFalse(d._preflight_view(7, {READY, HUMAN})['owned'])
                # ...but it is still the issue's latest: a request of a person repeats it.
                request()
                self.assertEqual(self.planned(d), [('preflight', 7)])
                self.runner.outputs.append(checked(False, [gap()]))
                d.tick()
                record = self.records()[-1]
                self.assertEqual((record['blocked'], record['labeled']), (True, 0))
                self.assertEqual(self.gh.labels[7], {READY, HUMAN})
                self.assertEqual(self.planned(d), [])
                self.gh.say(7, 'brushtyler', '/devloop recheck')
                self.runner.outputs.append(checked(True))
                d.tick()
                self.assertEqual(self.gh.labels[7], {READY})

    def test_an_escalation_of_another_class_keeps_its_block_and_the_recheck_is_answered(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.comment(7, markdown.render_escalation('I test falliscono.', cls='tests_failed'))
        self.gh.say(7, 'brushtyler', '/devloop recheck')
        self.assertEqual(self.planned(d), [('recheck_refused', 7)])
        self.assertEqual([a.kind for a in d.tick()], ['recheck_refused'])
        # No check runs, the block stays, and the answer says why.
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})
        body = self.gh.threads[7][-1]['body']
        for text in ('Controllo di prontezza non ripetuto', '`/devloop recheck`', '`brushtyler`',
                     'escalation di classe `tests_failed`'):
            self.assertIn(text, body)
        answer = self.gh.records(7, 'recheck')[-1]
        self.assertEqual((answer['request'], answer['by']), ('recheck', 'brushtyler'))
        self.assertTrue(any('recheck by brushtyler runs no readiness check: an escalation of '
                            'class tests_failed' in m for m in self.logs))
        # Answered once: the request is spent.
        self.assertEqual(self.planned(d), [])
        self.assertEqual(d.tick(), [])
        # A check run anyway leaves that block in place.
        self.runner.outputs.append(checked(True))
        d._do_preflight(7)
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})

    def test_agent_ready_added_again_on_another_block_is_answered_once_per_label(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.comment(7, markdown.render_escalation('I test falliscono.', cls='tests_failed'))
        self.gh.relabel(SUPERVISOR_LOGIN)
        self.assertEqual([a.kind for a in d.tick()], ['recheck_refused'])
        self.assertIn('`agent:ready` rimesso di `agy-agent[bot]`', self.gh.threads[7][-1]['body'])
        self.assertEqual(self.planned(d), [])
        # The label added again later is a new request: answered too.
        self.gh.relabel('brushtyler')
        self.assertEqual(self.planned(d), [('recheck_refused', 7)])
        self.assertEqual(len(self.runner.calls), 1)

    def test_an_escalation_of_the_same_class_keeps_the_block_the_checks(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.comment(7, markdown.render_escalation('Decide l\'operatore.',
                                                      cls='operator_decision'))
        self.gh.say(7, 'brushtyler', '/devloop recheck')
        self.assertEqual(self.planned(d), [('preflight', 7)])

    def test_a_block_added_again_by_someone_else_is_not_the_checks(self):
        d = self.make()
        self.blocked(d, gap())
        self.gh.label_log[(7, HUMAN)].append('brushtyler')
        self.gh.say(7, 'brushtyler', '/devloop recheck')
        self.assertEqual(self.planned(d), [('recheck_refused', 7)])
        d.tick()
        self.assertIn('aggiunto di nuovo', self.gh.threads[7][-1]['body'])
        self.assertEqual(self.gh.labels[7], {READY, HUMAN})

    def test_the_requests_are_documented(self):
        guide = docs.plan_section('docs/devloop/guide.md',
                                  'Readiness check before implementation', root=docs.ROOT)
        docs.assert_documented(self, guide, [
            'latest', 'Decisione per l\'operatore', 'escalation of another class',
            'Controllo di prontezza non ripetuto', 'answers once'], issues=(468,))
        protocol = docs.plan_section('docs/devloop/protocol.md', 'Readiness check records',
                                     root=docs.ROOT)
        docs.assert_documented(self, protocol, [
            'recheck_refused', '`recheck` marker', 'supervisor\'s identity', 'preflight_default'],
            issues=(468,))


if __name__ == '__main__':
    unittest.main()
