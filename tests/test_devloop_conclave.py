"""devloop: conclave of the three agents on a block (#141, part 2 of #114).

The agents are simulated (``FakeRunner``): the tests check the protocol, the durable record and
its binding, not the models."""

import dataclasses
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from devloop import (agents, conclave, config, markdown, planning, redact, review,  # noqa: E402
                     schema, workspace)
from devloop.dispatcher import Dispatcher  # noqa: E402
from devloop.planning import Snapshot, plan  # noqa: E402
from devloop.workspace import Workspace  # noqa: E402
from helpers import ssdlc_project  # noqa: E402
from helpers.devloop_fakes import SHA1, FakeGH, FakeIdentities, at  # noqa: E402
from test_devloop import CFG, FakeRunner, FakeWS, implemented, pr, receipt  # noqa: E402

MEMBERS = ('claude', 'codex', 'agy')
CCFG = dataclasses.replace(CFG, conclave=MEMBERS)
LOGINS = FakeIdentities.LOGINS
NEEDS_OPERATOR = 'agent:needs-operator'
SHA2 = 'b' * 40
RESOLUTION = 'Usare il formato JSON già documentato in `docs/x.md`, senza nuove chiavi.\n  $issue_number resta letterale.'


def proposal(text=RESOLUTION, reserved=False, question='', options=()):
    return {'reserved': reserved, 'resolution': '' if reserved else text,
            'reserved_reason': 'Riguarda i permessi dell\'app.' if reserved else '',
            'operator_question': question, 'options': list(options)}


def vote(choice='APPROVA', reason='Coerente con l\'issue.', reserved=False, question='',
         options=()):
    return {'vote': choice, 'reason': reason, 'reserved': reserved,
            'reserved_reason': 'Tocca il merge.' if reserved else '',
            'operator_question': question, 'options': list(options)}


class ThreadGH(FakeGH):
    """FakeGH whose comments keep an id, their author and their edits, per issue or PR. A
    comment written through an agent's identity (``FakeIdentities``) has that agent's login,
    any other the dispatcher's."""

    def __init__(self, head=SHA1):
        super().__init__(head=head)
        self.thread = {}
        self.next_id = 1000
        self._seen = 0
        self.extra_issue_comments = []

    def _author(self):
        acts = getattr(self, 'acts', [])
        fresh = len(acts) > self._seen and acts[-1][2] == 'comment'
        self._seen = len(acts)
        return LOGINS[acts[-1][0]] if fresh else LOGINS['dispatcher']

    def comment(self, number, body):
        super().comment(number, body)
        self.next_id += 1
        self.thread.setdefault(number, []).append(
            {'id': self.next_id, 'author': {'login': self._author()}, 'body': body})
        return self.next_id

    def say(self, number, body, login):
        self.next_id += 1
        self.thread.setdefault(number, []).append(
            {'id': self.next_id, 'author': {'login': login}, 'body': body})
        return self.next_id

    def edit_comment(self, comment_id, body):
        super().edit_comment(comment_id, body)
        for comments in self.thread.values():
            for comment in comments:
                if comment['id'] == comment_id:
                    comment['body'] = body

    def pr_comments(self, number):
        return [dict(c) for c in self.thread.get(number, [])]

    issue_comments = pr_comments

    def issue(self, number):
        return dict(super().issue(number),
                    labels=[{'name': n} for n in sorted(self.labels.get(number, ()))])

    def issues_with_label(self, label):
        return [{'number': n, 'title': f'Issue {n}', 'author': {'login': 'brushtyler'},
                 'labels': [{'name': x} for x in sorted(labels)]}
                for n, labels in sorted(self.labels.items()) if label in labels and n < 100]

    def conclaves(self, number):
        return [markdown.read_marker(c['body'], 'conclave') for c in self.thread.get(number, [])
                if markdown.read_marker(c['body'], 'conclave') is not None]


class HookRunner(FakeRunner):
    """FakeRunner that runs ``hooks[n]`` while its ``n``-th call (from 1) is running: what
    changes on GitHub during an agent's turn."""

    def __init__(self, *outputs, hooks=None):
        super().__init__(*outputs)
        self.hooks = hooks or {}

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        hook = self.hooks.get(len(self.calls) + 1)
        try:
            return super().run(agent, mode, prompt, schema_name, workdir, label)
        finally:
            if hook is not None:
                hook()


class ConclaveWS(FakeWS):
    def __init__(self, diff='', **kw):
        super().__init__(**kw)
        self.diff_text = diff
        self.copies = []

    def commit_copy(self, name, commit, branch=None):
        self.copies.append((name, commit, branch))
        return Path('/wt') / name

    def pending_diff(self, name, commit):
        return self.diff_text

    def save_evidence(self, name, text):
        self.evidence = getattr(self, 'evidence', {})
        self.evidence[name] = text
        return Path('/state/logs') / name


class Base(unittest.TestCase):
    cfg = CCFG

    def make(self, *outputs, ws=None, identities=True, cfg=None, hooks=None):
        self.gh = ThreadGH()
        self.ws = ws or ConclaveWS()
        self.runner = HookRunner(*outputs, hooks=hooks)
        self.logs = []
        self.d = Dispatcher(cfg or self.cfg, self.gh, self.ws, self.runner,
                            log=self.logs.append,
                            identities=FakeIdentities(self.gh) if identities else None)
        return self.d

    def block_pr(self, agent='claude', step='fix', head=SHA1, reason='Il formato non è deciso.',
                 login=None):
        """PR #100 in agent:needs-operator after its fixer stopped: the notice, with the
        agent's identity (``login``, without GitHub Apps)."""
        self.gh.labels[100] = {NEEDS_OPERATOR}
        body = markdown.publishable(
            '> [!NOTE]\n> **Agent:** x\n\n## Lavoro bloccato\n\n' + reason, 'notice',
            {'event': 'blocked', 'class': 'blocked', 'critical': True, 'to': 'operator',
             'agent': agent, 'reason': reason, 'step': step, 'head': head})
        return self.gh.say(100, body, login or LOGINS[agent])

    def target(self, labels=(NEEDS_OPERATOR,)):
        return pr(labels=labels)

    def run_conclave(self, times=3, target=None):
        for _ in range(times):
            self.d._do_conclave(target or self.target())

    def record(self, number=100):
        (found,) = self.gh.conclaves(number)
        return found


class UnanimousTests(Base):
    def test_proposal_and_two_approvals_are_unanimous_bound_to_block_content_and_head(self):
        self.make(proposal(), vote(), vote())
        block_id = self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.UNANIMOUS)
        self.assertEqual(record['block'], {'kind': 'blocked', 'comment': block_id,
                                           'agent': 'claude', 'step': 'fix'})
        self.assertEqual(record['hash'], conclave.text_hash(RESOLUTION))
        self.assertEqual(record['head'], SHA1)
        self.assertEqual(record['commit'], SHA1)
        self.assertEqual(record['proposal']['resolution'], RESOLUTION)
        self.assertEqual([v['vote'] for v in record['votes']], ['APPROVA', 'APPROVA'])
        # The proposer is the agent that stopped, the voters the other two.
        self.assertEqual([c[0] for c in self.runner.calls], ['claude', 'codex', 'agy'])
        self.assertEqual([c[1:3] for c in self.runner.calls],
                         [('review', 'conclave_proposal'), ('review', 'conclave_vote'),
                          ('review', 'conclave_vote')])
        # One comment, the dispatcher's, edited as the conclave went on; the labels stay.
        conclave_comments = [c for c in self.gh.thread[100]
                             if markdown.read_marker(c['body'], 'conclave')]
        self.assertEqual(len(conclave_comments), 1)
        self.assertEqual(conclave_comments[0]['author']['login'], LOGINS['dispatcher'])
        body = conclave_comments[0]['body']
        self.assertIn('**UNANIMOUS**', body)
        self.assertIn('Coerente con l\'issue.', body)
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        # Read back: valid and consistent; what is left is the restart of the work (#142), one
        # action, and no other conclave.
        self.assertEqual(conclave.consistent(record), (True, ''))
        view = self.d._conclave_view(100, self.gh.pr_comments(100), self.target())
        self.assertTrue(view['resume'])
        self.assertEqual(view['calls'], 1)
        # The calls ran on a disposable checkout of the block's commit.
        self.assertEqual(self.ws.copies[0], ('pr-100-conclave', SHA1, 'agent/issue-7'))
        self.assertEqual(self.ws.removed.count('pr-100-conclave'), 3)

    def test_a_blocked_implementer_convenes_the_conclave_on_its_issue(self):
        self.make(implemented('blocked'), proposal(), vote(), vote(),
                  ws=ConclaveWS(diff='diff --git a/x.py b/x.py\n+partial'))
        self.d._do_implement(7)
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        (notice,) = [c for c in self.gh.thread[7] if markdown.read_marker(c['body'], 'notice')]
        self.assertEqual(notice['author']['login'], LOGINS['claude'])
        items = self.d._conclave_items([])
        self.assertEqual([(i['number'], i['calls']) for i in items], [(7, 3)])
        actions = plan(Snapshot(prs=[], ready_issues=[], working_issues=[], conclaves=items),
                       self.cfg)
        self.assertEqual([(a.kind, a.number) for a in actions], [('conclave', 7)] * 3)
        for _ in actions:
            self.d._do_conclave(7)
        record = self.record(7)
        self.assertEqual(record['outcome'], conclave.UNANIMOUS)
        self.assertEqual(record['block']['step'], 'implement')
        self.assertIsNone(record['head'])
        self.assertEqual(record['diff_hash'], conclave.text_hash(self.ws.diff_text))
        # The uncommitted changes of the agent that stopped reach every member, read only.
        for call in self.runner.calls[1:]:
            self.assertIn('+partial', call[3])
            self.assertIn('Uncommitted changes of the agent that stopped (read only)', call[3])
        # Its reason is in the proposer's prompt.
        self.assertIn('unclear', self.runner.calls[1][3])

    def test_a_reviewer_needs_operator_round_opens_one_conclave_with_the_first_as_proposer(self):
        self.make(proposal(), vote(), vote())
        self.gh.labels[100] = {NEEDS_OPERATOR}
        for reviewer in ('agy', 'codex'):
            record = review.normalize_review(
                {'verdict': 'needs_operator', 'summary': 's', 'blocking_findings': [],
                 'non_blocking_findings': [], 'previous_findings': [],
                 'human_reason': f'Serve una scelta ({reviewer}).', 'checks_performed': []},
                previous_open=[], round_no=1, max_rounds=3, head_sha=SHA1, ci='pass',
                reviewer=reviewer)
            self.gh.say(100, markdown.render_review(record, '> [!NOTE]\n'), LOGINS[reviewer])
        self.gh.say(100, markdown.render_escalation('decisione', cls='reviewer_decision'),
                    LOGINS['dispatcher'])
        self.run_conclave()
        found = self.record()
        self.assertEqual(found['block'], {'kind': 'review', 'round': 1, 'reviewer': 'agy',
                                          'head': SHA1})
        self.assertEqual(found['outcome'], conclave.UNANIMOUS)
        self.assertEqual([c[0] for c in self.runner.calls], ['agy', 'claude', 'codex'])
        self.assertIn('Serve una scelta (agy).', self.runner.calls[0][3])

    def test_a_needs_operator_in_a_native_review_runs_the_conclave(self):
        # Since #325 the review of an agent with an app is the body of its native review: the
        # conclave the snapshot plans from it must also run from it.
        self.make(proposal(), vote(), vote())
        self.gh.labels[100] = {NEEDS_OPERATOR}
        record = review.normalize_review(
            {'verdict': 'needs_operator', 'summary': 's', 'blocking_findings': [],
             'non_blocking_findings': [], 'previous_findings': [],
             'human_reason': 'Serve una scelta (codex).', 'checks_performed': []},
            previous_open=[], round_no=1, max_rounds=3, head_sha=SHA1, ci='pass',
            reviewer='codex')
        self.gh.native_reviews = [
            {'id': 77, 'login': LOGINS['codex'], 'state': 'CHANGES_REQUESTED',
             'body': markdown.render_review(record, '> [!NOTE]\n'), 'submitted_at': at(1)}]
        self.gh.next_id += 1
        self.gh.thread.setdefault(100, []).append(
            {'id': self.gh.next_id, 'author': {'login': LOGINS['dispatcher']},
             'body': markdown.render_escalation('decisione', cls='reviewer_decision'),
             'created_at': at(2)})
        # Not a comment: only the native review carries the record.
        self.assertIsNone(self.d._review_block(self.gh.pr_comments(100), self.target()))
        items = self.d._conclave_items([self.target()])
        self.assertEqual([(i['number'], i['calls']) for i in items], [(100, 3)])
        self.run_conclave()
        found = self.record()
        self.assertEqual(found['block'], {'kind': 'review', 'round': 1, 'reviewer': 'codex',
                                          'head': SHA1})
        self.assertEqual(found['outcome'], conclave.UNANIMOUS)
        self.assertEqual([c[0] for c in self.runner.calls], ['codex', 'claude', 'agy'])
        self.assertIn('Serve una scelta (codex).', self.runner.calls[0][3])

    def needs_operator_by_codex(self):
        """The record of round 1 of PR #100 by codex, needs_operator, under a level 2 receipt
        that requires codex and agy: agy has not reviewed yet."""
        record = review.normalize_review(
            {'verdict': 'needs_operator', 'summary': 's', 'blocking_findings': [],
             'non_blocking_findings': [], 'previous_findings': [],
             'human_reason': 'Serve una scelta (codex).', 'checks_performed': []},
            previous_open=[], round_no=1, max_rounds=3, head_sha=SHA1, ci='pass',
            reviewer='codex')
        record['receipt'] = dict(receipt(), level=2, reviewers=['codex', 'agy'])
        return record

    def test_the_first_needs_operator_suspends_an_incomplete_round_and_opens_the_conclave(self):
        record = self.needs_operator_by_codex()
        self.assertEqual(planning.pending_reviewers([record]), ['agy'])
        # Without the conclave the round waits for agy, as today; with it, the first
        # needs_operator suspends the round: its verdict applies, no review of agy is planned.
        snap = Snapshot(prs=[pr(reviews=[record])], ready_issues=[], working_issues=[])
        self.assertEqual([(a.kind, a.number) for a in plan(snap, CFG)], [('review', 100)])
        self.assertEqual([(a.kind, a.number) for a in plan(snap, self.cfg)],
                         [('apply_review', 100)])
        self.make(proposal(), vote(), vote())
        self.gh.labels[100] = {'agent:review'}
        self.gh.say(100, markdown.render_review(record, '> [!NOTE]\n'), LOGINS['codex'])
        self.d._do_apply_review(pr(reviews=[record]))
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        # The verdict of codex is the durable reference of the block, codex the proposer.
        self.run_conclave()
        found = self.record()
        self.assertEqual(found['block'], {'kind': 'review', 'round': 1, 'reviewer': 'codex',
                                          'head': SHA1})
        self.assertEqual(found['outcome'], conclave.UNANIMOUS)
        self.assertEqual([c[0] for c in self.runner.calls], ['codex', 'claude', 'agy'])
        self.assertIn('Serve una scelta (codex).', self.runner.calls[0][3])

    def test_without_conclave_an_incomplete_round_opens_nothing(self):
        self.make(cfg=CFG)
        self.gh.labels[100] = {NEEDS_OPERATOR}
        self.gh.say(100, markdown.render_review(self.needs_operator_by_codex(), '> [!NOTE]\n'),
                    LOGINS['codex'])
        self.assertIsNone(self.d._review_block(self.gh.pr_comments(100), self.target()))


class OutcomeTests(Base):
    def test_one_block_vote_is_blocked_and_the_other_vote_is_still_collected(self):
        self.make(proposal(), vote('BLOCCA', 'Allarga il perimetro.'), vote())
        self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual([v['state'] for v in record['votes']], ['done', 'done'])

    def test_a_voter_in_timeout_is_a_block_not_an_abstention(self):
        self.make(proposal(), vote(), agents.AgentError('agy timed out after 3600s'))
        self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['votes'][1]['state'], 'missing')
        self.assertIn('timed out', record['votes'][1]['missing'])
        # No other escalation: the item was already the operator's.
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})

    def test_a_failed_proposer_is_blocked_and_nothing_is_voted(self):
        self.make(agents.AgentError('claude crashed'))
        self.block_pr()
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual([v['missing'] for v in record['votes']],
                         ['nessuna proposta da votare'] * 2)
        self.assertIn('crashed', record['proposal']['missing'])

    def test_a_proposer_that_flags_the_reserved_perimeter_ends_it_reserved(self):
        self.make(proposal(reserved=True, question='Concedere `contents: write`?',
                           options=['Sì, solo per questa PR', 'No']))
        self.block_pr()
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.RESERVED)
        self.assertEqual(len(self.runner.calls), 1)  # no votes, and no invented proposal
        self.assertEqual(record['proposal']['resolution'], '')
        body = self.gh.thread[100][-1]['body']
        self.assertIn('### Domanda per l\'operatore', body)
        self.assertIn('Concedere `contents: write`?', body)
        self.assertIn('- Sì, solo per questa PR', body)
        self.assertEqual(self.d._conclave_items([self.target()]), [])

    def test_a_voter_that_flags_the_perimeter_prevails_over_the_approvals(self):
        self.make(proposal(), vote(), vote(reserved=True, question='Chi mergia?',
                                          options=['L\'operatore']))
        self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.RESERVED)
        self.assertIn('Chi mergia?', self.gh.thread[100][-1]['body'])

    def test_a_first_voter_that_flags_the_perimeter_still_gets_the_second_vote(self):
        self.make(proposal(), vote(reserved=True, question='Chi decide il budget?',
                                   options=['L\'operatore']),
                  vote('BLOCCA', reserved=True, question='Chi mergia?'))
        self.block_pr()
        self.run_conclave(times=1)
        self.run_conclave(times=1)
        record = self.record()
        # Not concluded after the first vote: the second is still due.
        self.assertEqual(record['outcome'], '')
        self.assertEqual(self.d._conclave_items([self.target()])[0]['calls'], 1)
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.RESERVED)
        self.assertEqual([v['state'] for v in record['votes']], ['done', 'done'])
        self.assertEqual(len(self.runner.calls), 3)
        body = self.gh.thread[100][-1]['body']
        self.assertIn('Chi decide il budget?', body)
        self.assertIn('Chi mergia?', body)
        self.assertNotIn('non ancora richiesto', body)

    def test_a_reserved_flag_after_a_block_vote_still_prevails(self):
        self.make(proposal(), vote('BLOCCA'), vote('BLOCCA', reserved=True, question='Budget?'))
        self.block_pr()
        self.run_conclave()
        self.assertEqual(self.record()['outcome'], conclave.RESERVED)

    def test_outputs_beyond_the_limits_are_failed_contributions(self):
        self.make(proposal('x' * (conclave.MAX_RESOLUTION + 1)))
        self.block_pr()
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertIn('oltre i limiti', record['proposal']['missing'])


class HashTests(Base):
    def test_voters_receive_exactly_the_proposed_text_bound_by_its_hash(self):
        self.make(proposal(), vote(), vote())
        self.block_pr()
        self.run_conclave()
        record = self.record()
        digest = conclave.text_hash(RESOLUTION)
        for call in self.runner.calls[1:]:
            prompt = call[3]
            # The text verbatim (a `$name` in it is not substituted), and its hash.
            self.assertIn('\n' + RESOLUTION + '\n', prompt)
            self.assertIn(digest, prompt)
        self.assertEqual([v['proposal_hash'] for v in record['votes']], [digest, digest])

    def redacting(self, r):
        """The dispatcher's writes through ``r``, as devloop gives them (#583, #620)."""
        self.d.identities.redactor = r
        self.d.gh = redact.RedactingClient(self.gh, r)

    def test_a_new_proposal_is_redacted_before_its_hash(self):
        # #620: the voters get, and the record carries, the redacted text its hash binds; the
        # identifiers of the record stay, a service called as the step and a user called as
        # the agent included (R3-1).
        r = redact.Redactor(paths=[('STATE_DIR', '/srv/u-dl/.state')],
                            users=[('AGENT_USER', 'claude')], names=[('SERVICE_NAME', 'fix')])
        text = 'Rilanciare i test in /srv/u-dl/.state/worktrees/pr-100.'
        clean = 'Rilanciare i test in $STATE_DIR/worktrees/pr-100.'
        self.make(proposal(text), vote(), vote())
        self.redacting(r)
        self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.UNANIMOUS)
        self.assertEqual(record['proposal']['resolution'], clean)
        self.assertEqual(record['hash'], conclave.text_hash(clean))
        for call in self.runner.calls[1:]:
            self.assertIn('\n' + clean + '\n', call[3])
            self.assertIn(record['hash'], call[3])
            self.assertNotIn('/srv/', call[3])
        self.assertEqual((record['step'], record['block']['agent'], record['proposal']['agent']),
                         ('fix', 'claude', 'claude'))
        self.assertEqual(conclave.consistent(record), (True, ''))

    def test_a_recorded_proposal_with_host_details_is_not_updated(self):
        # #620: a proposal recorded before, which the redaction would change, stops the update
        # of its record: its hash is not computed again, and no vote is asked on another text.
        self.make(proposal('Rilanciare in /srv/u-dl/x.'), vote(), vote())
        self.block_pr()
        self.run_conclave(times=1)
        (comment,) = [c for c in self.gh.thread[100] if markdown.read_marker(c['body'],
                                                                              'conclave')]
        before = comment['body']
        self.redacting(redact.Redactor(paths=[('DEVLOOP_HOME', '/srv/u-dl')]))
        with self.assertRaises(redact.BoundTextChanged):
            self.run_conclave(times=1)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(comment['body'], before)

    def test_a_recorded_text_that_no_longer_matches_its_hash_gets_no_vote(self):
        self.make(proposal(), vote(), vote())
        self.block_pr()
        self.run_conclave(times=1)
        (comment,) = [c for c in self.gh.thread[100] if markdown.read_marker(c['body'],
                                                                              'conclave')]
        record = markdown.read_marker(comment['body'], 'conclave')
        record['proposal']['resolution'] = 'Un altro testo.'
        comment['body'] = conclave.render(record)
        self.run_conclave(times=1)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertTrue(any('does not hold' in line for line in self.logs))


class ResumeTests(Base):
    def test_a_concluded_block_never_convenes_a_second_conclave(self):
        self.make(proposal(), vote('BLOCCA'), vote())
        self.block_pr()
        self.run_conclave()
        # A replay of the same block, and a restart: nothing to plan, nothing runs.
        self.assertEqual(self.d._conclave_items([self.target()]), [])
        self.d._do_conclave(self.target())
        self.assertEqual(len(self.runner.calls), 3)
        self.assertEqual(len(self.gh.conclaves(100)), 1)

    def test_a_replayed_reviewer_verdict_is_the_same_block(self):
        self.make(proposal(), vote('BLOCCA'), vote())
        self.gh.labels[100] = {NEEDS_OPERATOR}
        record = review.normalize_review(
            {'verdict': 'needs_operator', 'summary': 's', 'blocking_findings': [],
             'non_blocking_findings': [], 'previous_findings': [], 'human_reason': 'Scelta.',
             'checks_performed': []}, previous_open=[], round_no=1, max_rounds=3,
            head_sha=SHA1, ci='pass', reviewer='codex')
        self.gh.say(100, markdown.render_review(record, '> [!NOTE]\n'), LOGINS['codex'])
        self.run_conclave()
        self.assertEqual(self.record()['outcome'], conclave.BLOCKED)
        # The verdict applied again (a replay announces it again): the same block.
        self.gh.say(100, markdown.render_escalation('decisione', cls='reviewer_decision'),
                    LOGINS['dispatcher'])
        self.assertEqual(self.d._conclave_items([self.target()]), [])

    def test_a_call_started_without_a_durable_result_counts_as_block_and_is_not_repeated(self):
        self.make(proposal(), RuntimeError('dispatcher crashed during the call'), vote())
        self.block_pr()
        self.run_conclave(times=1)
        with self.assertRaises(RuntimeError):
            self.run_conclave(times=1)
        self.assertEqual(self.record()['votes'][0]['state'], 'started')
        # The restart: one call left, the second vote; the first is a failure, not repeated.
        (item,) = self.d._conclave_items([self.target()])
        self.assertEqual(item['calls'], 1)
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['votes'][0]['missing'], conclave.STARTED_WITHOUT_RESULT)
        self.assertEqual([c[0] for c in self.runner.calls], ['claude', 'codex', 'agy'])

    def test_a_conclave_out_of_slots_resumes_at_the_next_tick(self):
        cfg = dataclasses.replace(CCFG, max_actions_per_tick=2)
        self.make(implemented('blocked'), proposal(), vote(), vote(), implemented(), cfg=cfg)
        self.d._do_implement(7)
        first = self.d.tick()
        self.assertEqual([(a.kind, a.number) for a in first], [('conclave', 7)] * 2)
        record = self.record(7)
        self.assertEqual(record['outcome'], '')
        self.assertEqual([v['state'] for v in record['votes']], ['done', 'pending'])
        second = self.d.tick()
        self.assertEqual([(a.kind, a.number) for a in second], [('conclave', 7)])
        self.assertEqual(self.record(7)['outcome'], conclave.UNANIMOUS)
        # The unanimous decision restarts the implementation once (#142), on the next tick.
        third = self.d.tick()
        self.assertEqual([(a.kind, a.number) for a in third], [('conclave', 7)])
        self.assertIsNotNone(self.record(7)['resumed'])
        self.assertEqual(self.d.tick(), [])
        # implement, proposal, two votes, the implementation restarted
        self.assertEqual(len(self.runner.calls), 5)

    def test_a_pr_head_that_moves_before_the_end_makes_it_blocked(self):
        self.make(proposal(), vote(), vote())
        self.block_pr()
        self.run_conclave(times=1)
        self.gh.head = SHA2
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertIn('head', record['stale'])
        self.assertEqual(len(self.runner.calls), 1)
        # The same block does not convene it again.
        self.assertIsNone(self.d._conclave_view(100, self.gh.pr_comments(100), self.target()))

    def test_a_pr_head_that_moves_during_the_last_vote_makes_it_blocked(self):
        self.make(proposal(), vote(), vote(), hooks={3: lambda: setattr(self.gh, 'head', SHA2)})
        self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertIn('head', record['stale'])
        self.assertEqual([v['state'] for v in record['votes']], ['done', 'done'])
        self.assertEqual(conclave.consistent(record), (True, ''))
        # Concluded: the same block does not convene it again.
        self.assertIsNone(self.d._conclave_view(100, self.gh.pr_comments(100), self.target()))

    def test_an_issue_text_that_changes_during_the_last_vote_makes_it_blocked(self):
        self.make(implemented('blocked'), proposal(), vote(), vote(),
                  hooks={4: lambda: self.gh.say(7, 'Nuovo requisito: usare YAML.',
                                                'brushtyler')})
        self.d._do_implement(7)
        for _ in range(3):
            self.d._do_conclave(7)
        record = self.record(7)
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertIn('issue', record['stale'])

    def test_a_new_file_that_changes_during_the_last_vote_makes_it_blocked(self):
        # Two contents of a new file that splitlines() would make alike: U+2028 inside a JSON
        # string (valid) and a LF in its place (invalid JSON).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'new.json').write_bytes('{"a": "x y"}\n'.encode())

            class FileWS(ConclaveWS):
                def pending_diff(self, name, commit):
                    return workspace._new_file_diff(root, 'new.json')

            self.make(proposal(), vote(), vote(), ws=FileWS(),
                      hooks={3: lambda: (root / 'new.json').write_bytes(b'{"a": "x\ny"}\n')})
            self.block_pr()
            self.run_conclave()
        record = self.record()
        # The content as it is, separator included, reached the members.
        self.assertIn('+{"a": "x y"}\n', self.runner.calls[0][3])
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertIn('non committate', record['stale'])
        self.assertEqual([v['state'] for v in record['votes']], ['done', 'done'])

    def test_a_reserved_flag_in_the_last_vote_prevails_over_a_context_that_changed(self):
        self.make(proposal(), vote(), vote(reserved=True, question='Chi mergia?'),
                  hooks={3: lambda: setattr(self.gh, 'head', SHA2)})
        self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.RESERVED)
        self.assertIn('head', record['stale'])

    def test_an_unexpected_error_with_parallel_actions_does_not_end_the_block(self):
        cfg = dataclasses.replace(CCFG, max_parallel_actions=2)
        self.make(implemented('blocked'), proposal(),
                  RuntimeError('dispatcher crashed during the call'), vote(), cfg=cfg)
        self.d._do_implement(7)
        notices = len(self.gh.thread[7])
        actions = self.d.tick()
        self.assertEqual([(a.kind, a.number) for a in actions], [('conclave', 7)] * 3)
        # No escalation: the block stays, and the call started without a result is settled
        # as a failure by the next action, not repeated.
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        self.assertEqual([c for c in self.gh.thread[7][notices:]
                          if markdown.read_marker(c['body'], 'notice')], [])
        record = self.record(7)
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['votes'][0]['missing'], conclave.STARTED_WITHOUT_RESULT)
        self.assertEqual(record['votes'][1]['state'], 'done')
        self.assertEqual([c[0] for c in self.runner.calls], ['claude', 'claude', 'codex', 'agy'])

    def test_a_pr_no_longer_at_the_commit_of_the_block_gets_a_blocked_conclave_at_once(self):
        self.make()
        self.block_pr(head=SHA2)
        self.run_conclave(times=1)
        record = self.record()
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(record['proposal']['state'], 'missing')

    def test_an_issue_text_that_changes_before_the_end_makes_it_blocked(self):
        self.make(implemented('blocked'), proposal(), vote(), vote())
        self.d._do_implement(7)
        self.d._do_conclave(7)
        self.gh.say(7, 'Nuovo requisito: usare YAML.', 'brushtyler')
        self.d._do_conclave(7)
        record = self.record(7)
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertIn('issue', record['stale'])

    def test_a_later_escalation_or_a_new_review_leaves_no_block(self):
        self.make()
        self.block_pr()
        self.gh.say(100, markdown.render_escalation('altro', cls='failure'),
                    LOGINS['dispatcher'])
        self.assertEqual(self.d._conclave_items([self.target()]), [])


class FallbackTests(Base):
    """codex and agy of the same kind, with different models: the configuration holds, and a
    fallback model of codex may coincide with the model of agy."""

    def config(self, fallback):
        codex = dataclasses.replace(
            CFG.agent('codex'),
            fallback_models=(config.FallbackModel(fallback, fallback, 'high'),))
        agy = dataclasses.replace(CFG.agent('agy'), kind='codex', model='other-model',
                                  model_label='other-model', effort='high')
        return dataclasses.replace(CCFG, agents=dict(CFG.agents, codex=codex, agy=agy))

    def unavailable(self):
        return agents.ModelUnavailable('quota exhausted', None, 'quota', None)

    def test_a_fallback_that_coincides_with_another_member_is_not_used(self):
        self.make(proposal(), self.unavailable(), vote(), cfg=self.config('other-model'))
        self.block_pr()
        self.run_conclave()
        record = self.record()
        # codex's model is unavailable and its fallback is agy's tuple: refused, so its vote is
        # missing (a BLOCCA), never a second contribution of the same tuple.
        self.assertEqual(record['outcome'], conclave.BLOCKED)
        self.assertEqual(record['votes'][0]['state'], 'missing')
        self.assertEqual([c[0] for c in self.runner.calls], ['claude', 'codex', 'agy'])
        self.assertEqual(record['votes'][1]['profile']['model'], 'other-model')
        self.assertEqual(conclave.consistent(record), (True, ''))

    def test_the_effective_profile_is_recorded_and_bounds_the_next_calls(self):
        self.make(proposal(), self.unavailable(), vote(), vote(),
                  cfg=self.config('third-model'))
        self.block_pr()
        self.run_conclave()
        record = self.record()
        self.assertEqual(record['outcome'], conclave.UNANIMOUS)
        self.assertEqual(record['proposal']['profile']['model'], CFG.agent('claude').model)
        self.assertEqual(record['votes'][0]['profile']['model'], 'third-model')
        self.assertEqual(record['votes'][0]['profile']['identity'], LOGINS['codex'])
        # A member resumed on a later tick is checked against what the record keeps: the
        # fallback codex used now excludes that tuple for agy.
        allowed = self.d._conclave_allowed(record, record['votes'][1])
        clash = dataclasses.replace(self.d.cfg.agent('agy'), model='third-model')
        self.assertFalse(allowed(clash))
        self.assertTrue(allowed(self.d.cfg.agent('agy')))

    def test_a_record_with_two_contributions_of_the_same_tuple_does_not_hold(self):
        self.make(proposal(), vote(), vote())
        self.block_pr()
        self.run_conclave()
        record = self.record()
        record['votes'][1]['profile'] = dict(record['votes'][0]['profile'],
                                             identity=LOGINS['agy'])
        ok, why = conclave.consistent(record)
        self.assertFalse(ok)
        self.assertIn('stessa', why)


class DisabledTests(Base):
    cfg = CFG

    def test_without_conclave_the_escalation_is_todays(self):
        self.make(implemented('blocked'))
        self.d._do_implement(7)
        self.assertEqual(self.gh.labels[7], {NEEDS_OPERATOR})
        self.assertEqual(self.d._conclave_items([]), [])
        snap = self.d.snapshot()
        self.assertEqual(snap.conclaves, [])
        self.assertEqual(self.d.tick(), [])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.conclaves(7), [])

    def test_the_distributed_configuration_leaves_it_disabled_with_an_example(self):
        self.assertEqual(ssdlc_project.load_by_role().conclave, ())
        text = (Path(config.__file__).parent / 'config.toml').read_text(encoding='utf-8')
        # #499: reviewer keys, read-only as a review.
        self.assertIn('# conclave = ["claude-reviewer", "codex-reviewer", "agy-reviewer"]', text)

    def test_without_distinct_identities_there_is_no_conclave(self):
        # Without GitHub Apps every agent acts with the operator's login.
        self.make(identities=False, cfg=CCFG)
        self.block_pr(login='brushtyler')
        self.assertEqual(self.d._conclave_items([self.target()]), [])
        self.assertTrue(any('not distinct by identity' in line for line in self.logs))

    def test_a_proposer_outside_the_three_has_no_conclave(self):
        other = dataclasses.replace(CFG.agent('claude'), key='other', model='other-model')
        self.make(cfg=dataclasses.replace(CCFG, agents=dict(CFG.agents, other=other)))
        self.gh.labels[100] = {NEEDS_OPERATOR}
        # Without an app of its own, its records come from a trusted user.
        body = markdown.publishable('> [!NOTE]\n\n## Lavoro bloccato\n\nx', 'notice',
                                    {'event': 'blocked', 'agent': 'other', 'reason': 'x',
                                     'step': 'fix', 'head': SHA1})
        self.gh.say(100, body, 'brushtyler')
        self.assertEqual(self.d._conclave_items([self.target()]), [])
        self.assertTrue(any('`other` is not a member' in line for line in self.logs))

    def test_the_configuration_requires_three_distinct_agents(self):
        data = ssdlc_project.data()
        self.assertEqual(config.parse_config(dict(data, conclave=list(MEMBERS))).conclave,
                         MEMBERS)
        for bad in (['claude', 'codex'], ['claude', 'codex', 'codex'],
                    ['claude', 'codex', 'nobody']):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.parse_config(dict(data, conclave=bad))
        same = dict(data, conclave=list(MEMBERS),
                    agents=dict(data['agents'], agy=dict(data['agents']['agy'],
                                                         kind=data['agents']['codex']['kind'],
                                                         model=data['agents']['codex']['model'],
                                                         effort=data['agents']['codex']['effort'])))
        with self.assertRaises(config.ConfigError):
            config.parse_config(same)
        self.assertIn('conclave', config.OVERLAY_KEYS)

    def test_the_schemas_have_every_property_required(self):
        for name in ('conclave_proposal', 'conclave_vote'):
            loaded = schema.load_schema(name)
            self.assertEqual(sorted(loaded['required']), sorted(loaded['properties']))
            self.assertIs(loaded['additionalProperties'], False)
        schema.validate(proposal(), schema.load_schema('conclave_proposal'))
        schema.validate(vote(), schema.load_schema('conclave_vote'))
        with self.assertRaises(schema.SchemaError):
            schema.validate(vote('ASTENUTO'), schema.load_schema('conclave_vote'))


class WorkspaceTests(unittest.TestCase):
    """The two git operations of the conclave, on a real repository with an origin."""

    def git(self, *args, cwd):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x',
                   GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
        return subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_the_pending_diff_and_the_checkout_of_a_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo = Path(tmp) / 'origin.git', Path(tmp) / 'repo'
            self.git('init', '-q', '--bare', '-b', 'main', str(origin), cwd=tmp)
            self.git('clone', '-q', str(origin), str(repo), cwd=tmp)
            (repo / 'a.py').write_text('a\n')
            self.git('add', '-A', cwd=repo)
            self.git('commit', '-q', '-m', 'root', cwd=repo)
            self.git('push', '-q', 'origin', 'HEAD:main', cwd=repo)
            ws = Workspace(repo, '.state', 'main')
            path = ws.new_branch('issue-7', 'agent/issue-7')
            commit = ws.head(path)
            # Nothing left by the agent, then a change and a new file.
            self.assertEqual(ws.pending_diff('issue-7', commit), '')
            (path / 'a.py').write_text('a\npartial\n')
            (path / 'new.py').write_text('n\n')
            diff = ws.pending_diff('issue-7', commit)
            self.assertIn('+partial', diff)
            # A new file comes with its content: the members cannot read it in the checkout.
            self.assertIn('+++ b/new.py\n@@ -0,0 +1,1 @@\n+n\n', diff)
            self.assertEqual(ws.pending_diff('issue-7', commit), diff)  # deterministic
            # A change to its content changes the diff, and so the hash bound in the record.
            (path / 'new.py').write_text('m\n')
            changed = ws.pending_diff('issue-7', commit)
            self.assertIn('+m\n', changed)
            self.assertNotEqual(conclave.text_hash(changed), conclave.text_hash(diff))
            # Separators other than LF stay as the file has them, and contents that would look
            # alike line by line still differ, by the sha256 of their bytes.
            seen = set()
            for data in (b'x\r\ny\n', b'x\ny\n', 'x y\n'.encode(), b'x\ry\n', b'x\ny'):
                (path / 'new.py').write_bytes(data)
                shown = ws.pending_diff('issue-7', commit)
                self.assertIn(hashlib.sha256(data).hexdigest(), shown)
                lines = data.decode().removesuffix('\n').split('\n')
                self.assertIn(''.join(f'+{line}\n' for line in lines), shown)
                seen.add(conclave.text_hash(shown))
            self.assertEqual(len(seen), 5)
            self.assertIn('+y\n\\ No newline at end of file\n', shown)
            (path / 'new.py').write_text('m\n')
            (path / 'blob.bin').write_bytes(b'\0\1\2')
            self.assertIn('binary or larger', ws.pending_diff('issue-7', commit))
            (path / 'blob.bin').unlink()
            # Read only: the worktree keeps its changes, untracked file included.
            self.assertTrue(ws.has_changes(path))
            self.assertIn('?? new.py', self.git('status', '--porcelain', cwd=path))
            self.assertEqual(ws.pending_diff('issue-8', commit), '')
            copy = ws.commit_copy('issue-7-conclave', commit)
            self.assertEqual(ws.head(copy), commit)
            self.assertEqual((copy / 'a.py').read_text(), 'a\n')
            ws.remove('issue-7-conclave')
            self.assertFalse(copy.exists())


class RecordTests(unittest.TestCase):
    def fresh(self):
        return conclave.new_record({'kind': 'blocked', 'comment': 5, 'agent': 'claude',
                                    'step': 'fix'}, 'fix', list(MEMBERS), 7, 'h' * 64, SHA1,
                                   SHA1, conclave.text_hash(''))

    def test_outcome_precedence(self):
        record = conclave.apply_proposal(self.fresh(), proposal())
        self.assertIsNone(conclave.outcome(record))
        conclave.apply_vote(record, 0, vote(), record['hash'])
        self.assertIsNone(conclave.outcome(record))
        conclave.apply_vote(record, 1, vote(), record['hash'])
        self.assertEqual(conclave.outcome(record), conclave.UNANIMOUS)
        stale =conclave.make_stale(conclave.apply_proposal(self.fresh(), proposal()), 'x')
        self.assertEqual(conclave.outcome(stale), conclave.BLOCKED)

    def test_the_marker_round_trips_and_a_malformed_one_is_refused(self):
        record = conclave.apply_proposal(self.fresh(), proposal())
        body = conclave.render(record)
        self.assertEqual(markdown.read_marker(body, 'conclave'), record)
        broken = dict(record, outcome='MAYBE')
        self.assertIsNone(markdown.read_marker(markdown.publishable('x', 'conclave', broken),
                                               'conclave'))


if __name__ == '__main__':
    unittest.main()
