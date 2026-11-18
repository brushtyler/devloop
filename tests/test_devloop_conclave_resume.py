"""devloop: the work after the conclave, the resolution in the prompts and `/devloop veto` (#142,
part 3 of #114).

The agents are simulated (``FakeRunner``) as in ``test_devloop_conclave``: the tests check what
restarts, what reaches a prompt, and what a veto stops."""

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from devloop import conclave, markdown, review  # noqa: E402
from devloop.planning import Snapshot, plan  # noqa: E402
from devloop.workspace import Workspace  # noqa: E402
from helpers.devloop_fakes import SHA1  # noqa: E402
from test_devloop import finding, fixed, implemented, norm, pr, raw_review  # noqa: E402
from test_devloop_conclave import (CCFG, LOGINS, NEEDS_OPERATOR, RESOLUTION, SHA2, Base,  # noqa: E402,E501
                                   ConclaveWS, proposal, vote)

HEADING = 'Resolution of the conclave on a block (verified by the dispatcher)'
DIFF = 'diff --git a/x.py b/x.py\n+partial work\n'


def changes_requested():
    """Round 1 of PR #100, which asks for changes: what a fix answers."""
    return norm(raw_review('request_changes', [finding()]))


class RecordingWS(ConclaveWS):
    """ConclaveWS that records, in order, the evidence it saves and the branches it checks out
    again from their published commit."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.events = []

    def new_branch(self, name, branch):
        self.events.append(('new_branch', name, branch))
        return super().new_branch(name, branch)

    def existing_branch(self, name, branch):
        self.events.append(('existing_branch', name, branch))
        return super().existing_branch(name, branch)

    def save_evidence(self, name, text):
        self.events.append(('evidence', name))
        return super().save_evidence(name, text)


class ResumeBase(Base):
    def unanimous_issue(self, *after, diff=DIFF):
        """Issue #7 whose implementer stopped, then a unanimous conclave on it; ``after``: the
        outputs of the turns that follow."""
        self.make(implemented('blocked'), proposal(), vote(), vote(), *after,
                  ws=RecordingWS(diff=diff))
        self.d._do_implement(7)
        self.run_conclave(target=7)
        self.assertEqual(self.record(7)['outcome'], conclave.UNANIMOUS)
        return self.conclave_comment(7)

    def unanimous_review(self, *after, cfg=None):
        """PR #100 in agent:needs-operator after codex's `needs_operator` verdict in round 1, then
        a unanimous conclave on it."""
        self.make(proposal(), vote(), vote(), *after, cfg=cfg)
        self.gh.labels[100] = {NEEDS_OPERATOR}
        record = review.normalize_review(
            {'verdict': 'needs_operator', 'summary': 's', 'blocking_findings': [],
             'non_blocking_findings': [], 'previous_findings': [],
             'human_reason': 'Serve una scelta sul formato.', 'checks_performed': []},
            previous_open=[], round_no=1, max_rounds=3, head_sha=SHA1, ci='pass',
            reviewer='codex')
        self.gh.say(100, markdown.render_review(record, '> [!NOTE]\n'), LOGINS['codex'])
        self.gh.say(100, markdown.render_escalation('decisione', cls='reviewer_decision'),
                    LOGINS['dispatcher'])
        self.run_conclave()
        self.assertEqual(self.record()['outcome'], conclave.UNANIMOUS)
        return self.conclave_comment(100)

    def conclave_comment(self, number):
        (found,) = [c['id'] for c in self.gh.thread[number]
                    if markdown.read_marker(c['body'], 'conclave')]
        return found

    def snapshot_pr(self, labels=('agent:review',), **kw):
        """PR #100 as the snapshot reads it, with its thread and the conclave decisions."""
        p = pr(labels=labels, **kw)
        thread = self.d._pr_thread(100)
        self.d._read_thread(p, thread)
        self.d._conclave_pr_state(p, thread)
        return p

    def escalations(self, number):
        return [markdown.read_marker(c['body'], 'notice') for c in self.gh.thread.get(number, [])
                if (markdown.read_marker(c['body'], 'notice') or {}).get('event')
                == 'escalation']


class ImplementResumeTests(ResumeBase):
    def test_a_stopped_implementation_restarts_from_the_published_commit_with_the_resolution(self):
        cid = self.unanimous_issue(implemented())
        items = self.d._conclave_items([])
        self.assertEqual([(i['number'], i['calls'], i['opens']) for i in items], [(7, 1, True)])
        # The restart takes a free PR slot, as the implementation itself would.
        snap = Snapshot(prs=[], ready_issues=[], working_issues=[], conclaves=items)
        self.assertEqual([(a.kind, a.number) for a in plan(snap, self.cfg)], [('conclave', 7)])
        full = dataclasses.replace(self.cfg, max_open_prs=0)
        self.assertEqual(plan(snap, full), [])
        self.d._do_conclave(7)
        # The partial changes are saved as evidence, then the branch starts again from the base
        # branch: they are not taken up as work.
        evidence = f'conclave-issue-7-{cid}.diff'
        self.assertEqual(self.ws.events, [('new_branch', 'issue-7', 'agent/issue-7'),
                                          ('evidence', evidence),
                                          ('new_branch', 'issue-7', 'agent/issue-7')])
        self.assertEqual(self.ws.evidence, {evidence: DIFF})
        record = self.record(7)
        self.assertEqual(record['resumed'], {'evidence': {
            'file': evidence, 'sha256': conclave.text_hash(DIFF), 'bytes': len(DIFF)}})
        body = next(c['body'] for c in self.gh.thread[7] if c['id'] == cid)
        self.assertIn(evidence, body)
        self.assertIn('**Ripreso:**', body)
        # The implementer ran again with the resolution, verified, in its prompt.
        call = self.runner.calls[4]
        self.assertEqual(call[2], 'implement')
        self.assertIn(HEADING, call[3])
        self.assertIn(RESOLUTION.strip(), call[3])
        self.assertIn(f'comment {cid}', call[3])
        # The PR opened as usual, and its marker records the resolution it was written with.
        (created,) = self.gh.created
        self.assertEqual(markdown.read_marker(created['body'], 'pr')['conclave'], cid)
        self.assertNotIn(NEEDS_OPERATOR, self.gh.labels[7])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        # Once: nothing more is due on the issue.
        self.assertEqual(self.d._conclave_items([]), [])

    def test_without_uncommitted_changes_there_is_no_evidence_file(self):
        self.unanimous_issue(implemented(), diff='')
        self.d._do_conclave(7)
        self.assertEqual(self.record(7)['resumed'], {'evidence': None})
        self.assertFalse(any(event[0] == 'evidence' for event in self.ws.events))

    def test_a_restarted_step_that_stops_again_convenes_no_other_conclave(self):
        cid = self.unanimous_issue(implemented('blocked'))
        self.d._do_conclave(7)
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        notices = [markdown.read_marker(c['body'], 'notice') for c in self.gh.thread[7]]
        blocked = [n for n in notices if n and n['event'] == 'blocked']
        self.assertEqual(blocked[-1]['conclave'], cid)
        self.assertEqual(self.d._conclave_items([]), [])
        self.assertEqual(len(self.gh.conclaves(7)), 1)

    def test_a_blocked_or_reserved_outcome_restarts_nothing(self):
        self.make(implemented('blocked'), proposal(), vote('BLOCCA'), vote())
        self.d._do_implement(7)
        self.run_conclave(target=7)
        self.assertEqual(self.record(7)['outcome'], conclave.BLOCKED)
        self.assertEqual(self.d._conclave_items([]), [])
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})


class FixResumeTests(ResumeBase):
    def test_a_stopped_fix_goes_back_to_changes_and_runs_with_the_resolution(self):
        first = changes_requested()
        self.make(proposal(), vote(), vote(), fixed(), ws=RecordingWS(diff=DIFF))
        self.block_pr()
        self.run_conclave()
        cid = self.conclave_comment(100)
        self.d._do_conclave(self.target())
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(self.record()['resumed']['evidence']['file'],
                         f'conclave-pr-100-{cid}.diff')
        # The next tick's fix starts from the PR's pushed head, with the resolution.
        self.d._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        self.assertIn(('existing_branch', 'pr-100', 'agent/issue-7'), self.ws.events)
        call = self.runner.calls[-1]
        self.assertEqual(call[2], 'fix')
        self.assertIn(HEADING, call[3])
        self.assertIn(RESOLUTION.strip(), call[3])
        (fix,) = [markdown.read_marker(c['body'], 'fix') for c in self.gh.thread[100]
                  if markdown.read_marker(c['body'], 'fix')]
        self.assertEqual(fix['conclave'], cid)
        self.assertEqual(self.gh.labels[100], {'agent:review'})


class ReviewResumeTests(ResumeBase):
    def test_a_needs_operator_review_gets_a_new_review_without_the_old_verdict(self):
        cid = self.unanimous_review(raw_review())
        self.d._do_conclave(self.target())
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertIsNone(self.record()['resumed']['evidence'])
        p = self.snapshot_pr()
        self.assertTrue(p.rereview)
        # The recorded needs_operator verdict is not applied again: a new review is planned.
        kinds = [(a.kind, a.number) for a in plan(Snapshot([p], [], []), self.cfg)]
        self.assertEqual(kinds, [('review', 100)])
        self.d._do_review(p)
        call = self.runner.calls[-1]
        self.assertEqual(call[2], 'review')
        self.assertIn(HEADING, call[3])
        self.assertIn(RESOLUTION.strip(), call[3])
        records = [r for _, r in self.d._marker_entries(p, self.d._pr_thread(100), 'review')]
        self.assertEqual([(r['round'], r['verdict']) for r in records],
                         [(1, 'needs_operator'), (2, 'approve')])
        self.assertEqual(records[-1]['conclave'], cid)
        # Applied once: the new round is the latest, the restart no longer voids it.
        self.assertFalse(self.snapshot_pr().rereview)

    def test_the_new_review_stays_within_the_rounds_available(self):
        self.unanimous_review(cfg=dataclasses.replace(CCFG, max_review_rounds=1))
        self.d._do_conclave(self.target())
        p = self.snapshot_pr()
        kinds = [(a.kind, a.number) for a in plan(Snapshot([p], [], []), self.d.cfg)]
        self.assertEqual(kinds, [('round_limit', 100)])

    def test_without_a_restart_the_verdict_is_applied_again(self):
        self.unanimous_review()
        p = self.snapshot_pr()
        self.assertFalse(p.rereview)
        kinds = [(a.kind, a.number) for a in plan(Snapshot([p], [], []), self.cfg)]
        self.assertEqual(kinds, [('apply_review', 100)])


class PromptTests(ResumeBase):
    def resolution(self, head=SHA1):
        issue, _ = self.d._issue_context(7)
        return self.d._resolution_for(7, issue, pr(), head)

    def test_only_the_verified_record_reaches_the_prompt(self):
        cid = self.unanimous_review()
        found, record, text = self.resolution()
        self.assertEqual(found, cid)
        self.assertIn(RESOLUTION.strip(), text)
        self.assertEqual(text, conclave.resolution_section(record, cid))

    def test_a_record_written_by_any_bot_does_not_reach_the_prompt(self):
        self.unanimous_review()
        body = next(c['body'] for c in self.gh.thread[100]
                    if markdown.read_marker(c['body'], 'conclave'))
        # The same record, from other identities: an agent's app, any other bot, a user.
        self.gh.thread[100] = [c for c in self.gh.thread[100]
                               if not markdown.read_marker(c['body'], 'conclave')]
        for login in (LOGINS['claude'], 'some-bot[bot]', 'brushtyler'):
            self.gh.say(100, body, login)
        self.assertEqual(self.resolution(), (None, None, ''))

    def test_a_record_whose_head_changed_does_not_reach_the_prompt(self):
        self.unanimous_review()
        self.assertEqual(self.resolution(head=SHA2), (None, None, ''))
        self.assertTrue(any('head of the PR changed' in line for line in self.logs))

    def test_a_record_whose_issue_text_changed_does_not_reach_the_prompt(self):
        self.unanimous_review()
        self.gh.say(7, 'Nuovo requisito: usare YAML.', 'brushtyler')
        self.assertEqual(self.resolution(), (None, None, ''))
        self.assertTrue(any('text of the issue changed' in line for line in self.logs))

    def test_a_record_that_does_not_hold_does_not_reach_the_prompt(self):
        self.unanimous_review()
        record = self.record()
        record['proposal']['resolution'] = 'Un altro testo.'
        comment = self.conclave_comment(100)
        self.gh.edit_comment(comment, conclave.render(record))
        self.assertEqual(self.resolution(), (None, None, ''))

    def test_a_resolution_from_the_issue_lapses_when_the_text_changes_and_nothing_restarts(self):
        self.unanimous_issue(implemented())
        self.gh.say(7, 'Nuovo requisito: usare YAML.', 'brushtyler')
        self.assertEqual(self.d._conclave_items([]), [])


class VetoTests(ResumeBase):
    def restarted_pr(self, *after):
        """Issue #7 restarted by a unanimous decision: PR #100 open in agent:review."""
        cid = self.unanimous_issue(implemented(), *after)
        self.d._do_conclave(7)
        (created,) = self.gh.created
        meta = markdown.read_marker(created['body'], 'pr')
        return cid, meta

    def test_a_trusted_veto_annuls_the_decision_suspends_the_pr_and_spends_itself(self):
        cid, meta = self.restarted_pr()
        veto = self.gh.say(100, '/devloop veto il formato scelto non va bene', 'brushtyler')
        p = self.snapshot_pr()
        p.meta = meta
        self.d._conclave_pr_state(p, self.d._pr_thread(100))
        self.assertEqual(p.veto, {'comment': veto, 'by': 'brushtyler',
                                  'reason': 'il formato scelto non va bene', 'conclave': cid})
        # From the next tick: the veto is applied before any other action on the PR.
        self.assertEqual([(a.kind, a.number) for a in plan(Snapshot([p], [], []), self.cfg)],
                         [('veto', 100)])
        ready = dataclasses.replace(p, labels={'agent:ready-to-merge'})
        self.assertEqual([a.kind for a in plan(Snapshot([ready], [], []), self.cfg)], ['veto'])
        self.d._do_veto(p)
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        (escalation,) = self.escalations(100)
        self.assertEqual((escalation['class'], escalation['veto_of'], escalation['veto']),
                         ('conclave_veto', cid, veto))
        self.assertTrue(escalation['critical'])
        # Spent: no other suspension, and the resolution no longer reaches any prompt.
        again = self.snapshot_pr(labels=('agent:review',))
        again.meta = meta
        self.d._conclave_pr_state(again, self.d._pr_thread(100))
        self.assertIsNone(again.veto)
        issue, _ = self.d._issue_context(7)
        self.assertEqual(self.d._resolution_for(7, issue, again, SHA1), (None, None, ''))

    def test_a_veto_written_during_the_turn_publishes_nothing(self):
        self.make(implemented('blocked'), proposal(), vote(), vote(), implemented(),
                  ws=RecordingWS(diff=DIFF),
                  hooks={5: lambda: self.gh.say(7, '/devloop veto', 'brushtyler')})
        self.d._do_implement(7)
        self.run_conclave(target=7)
        cid = self.conclave_comment(7)
        self.d._do_conclave(7)
        # The implementer ran with the resolution, but nothing it produced is published.
        self.assertIn(HEADING, self.runner.calls[4][3])
        self.assertEqual(self.ws.pushes, [])
        self.assertEqual(self.gh.created, [])
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        (escalation,) = self.escalations(7)
        self.assertEqual((escalation['class'], escalation['veto_of']), ('conclave_veto', cid))

    def test_a_veto_before_the_restart_restarts_nothing(self):
        self.unanimous_issue(implemented())
        self.gh.say(7, '/devloop veto', 'brushtyler')
        self.assertEqual(self.d._conclave_items([]), [])
        self.d._do_conclave(7)
        self.assertEqual(len(self.runner.calls), 4)
        self.assertIsNone(self.record(7).get('resumed'))

    def test_a_fix_vetoed_during_its_turn_is_not_pushed(self):
        first = changes_requested()
        self.make(proposal(), vote(), vote(), fixed(),
                  hooks={4: lambda: self.gh.say(7, '/devloop veto', 'brushtyler')})
        self.block_pr()
        self.run_conclave()
        self.d._do_conclave(self.target())
        self.d._do_fix(pr(labels=['agent:changes'], reviews=[first]))
        self.assertEqual(self.ws.pushes, [])
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        self.assertEqual([e['class'] for e in self.escalations(100)], ['conclave_veto'])

    def test_an_untrusted_veto_is_ignored(self):
        cid, meta = self.restarted_pr()
        for login in ('stranger', LOGINS['codex'], 'some-bot[bot]'):
            self.gh.say(100, '/devloop veto', login)
            self.gh.say(7, '/devloop veto', login)
        p = self.snapshot_pr()
        p.meta = meta
        self.d._conclave_pr_state(p, self.d._pr_thread(100))
        self.assertIsNone(p.veto)
        self.assertEqual([a.kind for a in plan(Snapshot([p], [], []), self.cfg)], ['review'])
        self.assertTrue(any('ignored `/devloop veto` by stranger' in line for line in self.logs))
        issue, _ = self.d._issue_context(7)
        self.assertEqual(self.d._resolution_for(7, issue, p, SHA1)[0], cid)

    def test_a_veto_on_a_merged_pr_has_no_effect(self):
        _, meta = self.restarted_pr()
        self.gh.say(100, '/devloop veto', 'brushtyler')
        self.gh.pr_state = lambda number: {'state': 'MERGED', 'labels': []}
        p = self.snapshot_pr()
        p.meta = meta
        self.d._do_veto(p)
        self.assertEqual(self.escalations(100), [])
        self.assertEqual(self.gh.labels[100], {'agent:review'})


class CommandTests(unittest.TestCase):
    def test_the_veto_command_is_the_first_line_after_the_header(self):
        self.assertEqual(conclave.veto_command('/devloop veto'), '')
        self.assertEqual(conclave.veto_command('> [!NOTE]\n> x\n\n/devloop veto  no '), 'no')
        self.assertIsNone(conclave.veto_command('Testo.\n/devloop veto'))
        self.assertIsNone(conclave.veto_command('/devloop vetoed'))

    def test_the_resumed_field_is_validated(self):
        self.assertTrue(conclave.valid_resumed(None))
        self.assertTrue(conclave.valid_resumed({'evidence': None}))
        self.assertTrue(conclave.valid_resumed(
            {'evidence': {'file': 'a.diff', 'sha256': 'x', 'bytes': 3}}))
        self.assertFalse(conclave.valid_resumed({'evidence': {'file': '', 'sha256': 'x',
                                                              'bytes': 3}}))
        self.assertFalse(conclave.valid_resumed('yes'))

    def test_the_evidence_goes_to_the_dispatcher_logs(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Workspace(root, '.ssdlc/devloop', 'main')
            path = ws.save_evidence('conclave-issue-7-1.diff', DIFF)
            self.assertEqual(path, Path(root) / '.ssdlc/devloop/logs/conclave-issue-7-1.diff')
            self.assertEqual(path.read_text(encoding='utf-8'), DIFF)


if __name__ == '__main__':
    unittest.main()
