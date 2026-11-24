"""devloop: a decision of the operator on the trust policy is taken on the issue (#472).

A PR that carries out faithfully a decision of the operator that names what it changes (a class
of ``NON_CRITICAL``, a ``/devloop`` command, a condition of the merge) is merged like any other
of its level; a change of the rule itself still goes to the operator. The readiness check
declares whether the issue touches the trust policy, and blocks with the question to the operator
an issue that no decision covers, before the implementation. A comment in which the operator
approves the change counts as a decision for the readiness check and the reviewers when a
trusted user wrote it in person, after the latest change of the issue's text and before the
takeover: not the supervisor's identity, nor an app of devloop, nor a text with the attribution
header."""

import dataclasses
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from devloop import markdown, schema
from devloop.config import MergePolicy
from devloop.context import (APPROVE_COMMAND, PROMPT_DIR, approve_command,
                             format_operator_decisions, issue_decisions, refused_approvals,
                             text_changed_at, trust_policy_rule)
from devloop.github import GitHub
from devloop.dispatch_preflight import GAP_TEXTS, normalize_preflight
from devloop.dispatcher import Dispatcher, preflight_hash
from devloop.planning import plan
from devloop.supervision import NON_CRITICAL, preflight_class
from helpers.devloop_fakes import FakeGH
from test_devloop import (CFG, REV, REV_NAME, SHA1, FakeRunner, FakeWS, norm, pr, raw_review,
                          receipt)
from test_devloop_merge import MERGE, STRICT, Case, MergeGH, header, records
from test_devloop_preflight import NEEDS_OPERATOR, PCFG, READY, IssueGH, checked, gap
from test_devloop_trust_loop import OPERATOR as OPERATOR_LABEL, TrustGH, trust
from test_devloop_zero import changes

OPERATOR = 'brushtyler'
# The question devloop writes, in the language of the tests' project (Italian, #632).
TRUST_QUESTION = GAP_TEXTS.texts(CFG.language)('trust_question')
SUPERVISOR_LOGIN = 'brushtyler-devloop-supervisor[bot]'
WORKING = CFG.labels.working
CREATED = '2026-11-19T09:00:00Z'
EDITED = '2026-11-19T10:00:00Z'
APPROVED = '2026-11-19T11:00:00Z'
TAKEN = '2026-11-19T12:00:00Z'
LATER = '2026-11-19T13:00:00Z'
APPROVAL_TEXT = 'Approvo la modifica della policy di fiducia descritta qui.'
# The decision as a command (#483): the first line, then the text of the decision.
APPROVAL = f'{APPROVE_COMMAND}\n{APPROVAL_TEXT}'
HEADER = '> [!NOTE]\n> **Agent:** Claude Code (CLI v9.9)\n> **Model:** m\n\n'
DECISIONS = '## Decisions of the operator on the issue'
NONE = ('None: no comment on the issue counts as a decision of the operator. An approval '
        'written without `/devloop approve` is not one.')


def history(edited=EDITED, renamed=(), taken=TAKEN, complete=True):
    """The issue as ``issue_history`` reads it: opened, edited at ``edited``, taken on by
    devloop at ``taken`` (no takeover with None), read in full unless ``complete`` is False."""
    labeled = [('agent:ready', OPERATOR, CREATED)]
    if taken:
        labeled.append((WORKING, OPERATOR, taken))
    return {'title': 'Issue 7', 'body': 'Do the thing.', 'author': OPERATOR,
            'last_edited_at': edited, 'editor': OPERATOR if edited else None,
            'labeled': labeled, 'renamed': list(renamed), 'complete': complete}


def comment(at=APPROVED, body=APPROVAL, login=OPERATOR, updated=None):
    """A comment as ``issue_comments`` reads it, written at ``at`` and last changed at
    ``updated`` (``at`` by default: never edited)."""
    return {'author': {'login': login}, 'body': body, 'created_at': at,
            'updated_at': updated or at}


def decided(comments, hist=None, trusted=(OPERATOR,), apps=(SUPERVISOR_LOGIN,), taken=False):
    return issue_decisions(comments, hist or history(), set(trusted), set(apps), WORKING,
                           taken=taken)


class IssueDecisionsTests(unittest.TestCase):
    """Which comments of an issue may record a decision of the operator."""

    def test_an_approval_after_the_text_and_before_the_takeover_counts(self):
        approval = comment()
        for taken in (False, True):
            with self.subTest(review=taken):
                self.assertEqual(decided([approval], taken=taken), [approval])
        # Never edited: the comment only has to come before the takeover.
        self.assertEqual(decided([approval], history(edited=None)), [approval])

    def test_a_change_of_the_text_after_the_comment_drops_it(self):
        approval = comment()
        for hist in (history(edited='2026-11-19T11:30:00Z'),
                     history(renamed=[(OPERATOR, '2026-11-19T11:30:00Z')]),
                     # The same second as the change is not after it.
                     history(edited=APPROVED)):
            with self.subTest(hist=hist):
                self.assertGreaterEqual(text_changed_at(hist), APPROVED)
                self.assertEqual(decided([approval], hist), [])
                self.assertEqual(decided([approval], hist, taken=True), [])
        # A change after the takeover: the reviewers get no decision at all.
        self.assertEqual(decided([approval], history(edited=LATER), taken=True), [])
        self.assertEqual(decided([comment(at='2026-11-19T13:30:00Z')], history(edited=LATER),
                                 taken=True), [])

    def test_the_supervisor_and_the_apps_of_devloop_never_decide(self):
        # Not even with their login among the trusted users.
        trusted = (OPERATOR, SUPERVISOR_LOGIN, 'claude-agent[bot]')
        for login in (SUPERVISOR_LOGIN, 'claude-agent[bot]'):
            with self.subTest(login=login):
                self.assertEqual(decided([comment(login=login)], trusted=trusted,
                                         apps=(SUPERVISOR_LOGIN, 'claude-agent[bot]')), [])
        # The supervisor or an agent writing with the operator's login: the header tells it.
        self.assertEqual(decided([comment(body=HEADER + APPROVAL)]), [])
        # Anyone outside trusted_users.
        self.assertEqual(decided([comment(login='stranger')]), [])

    def test_a_comment_after_the_takeover_or_without_its_time_does_not_count(self):
        for at in (TAKEN, LATER, None, ''):
            with self.subTest(at=at):
                self.assertEqual(decided([comment(at=at)]), [])
        # Before the takeover, after the change: only that one counts, in order.
        early, late = comment(at='2026-11-19T10:30:00Z'), comment(at=LATER)
        self.assertEqual(decided([early, comment(at='2026-11-19T09:30:00Z'), late]), [early])

    def test_a_comment_rewritten_after_the_takeover_does_not_count(self):
        # Written before the takeover, turned into an approval after it: its text is read as it
        # is now, so it is not a decision taken before the takeover (#472).
        for updated in (TAKEN, LATER):
            rewritten = comment(updated=updated)
            for taken in (False, True):
                with self.subTest(updated=updated, review=taken):
                    self.assertEqual(decided([rewritten], taken=taken), [])
        # Edited before the takeover, it still counts.
        edited = comment(updated='2026-11-19T11:30:00Z')
        self.assertEqual(decided([edited], taken=True), [edited])
        # Before the takeover, when the readiness check reads it, there is no bound yet.
        self.assertEqual(decided([comment(updated=LATER)], history(taken=None)),
                         [comment(updated=LATER)])
        # A comment whose latest change is not known does not count.
        for updated in (None, ''):
            unknown = dict(comment(), updated_at=updated)
            with self.subTest(updated=updated):
                self.assertEqual(decided([unknown]), [])
        missing = comment()
        del missing['updated_at']
        self.assertEqual(decided([missing]), [])

    def test_a_review_needs_the_takeover(self):
        approval = comment()
        self.assertEqual(decided([approval], history(taken=None), taken=True), [])
        # The readiness check runs before it: no bound.
        self.assertEqual(decided([approval], history(taken=None)), [approval])
        self.assertEqual(decided([comment(at=LATER)], history(taken=None)),
                         [comment(at=LATER)])
        # A takeover before the latest change is not the takeover of this text.
        self.assertEqual(decided([approval], history(taken='2026-11-19T09:30:00Z'),
                                 taken=True), [])

    def test_the_section_lists_them_or_says_there_are_none(self):
        self.assertEqual(format_operator_decisions([]), f'{DECISIONS}\n\n{NONE}')
        text = format_operator_decisions([comment()])
        self.assertTrue(text.startswith(DECISIONS + '\n\n'))
        self.assertIn(f'### Comment by {OPERATOR}, {APPROVED}\n\n{APPROVAL}', text)
        self.assertIn('A decision of the operator, as one written in the body of the issue',
                      text)
        self.assertIn('It covers only what its text approves', text)


class DecisionGH(FakeGH):
    """Issue #7 with ``thread`` as its comments and ``hist`` as its history."""

    def __init__(self, thread=(), hist=None):
        super().__init__()
        self.thread, self.hist = list(thread), hist or history()

    def issue_history(self, number):
        return dict(self.hist)

    def issue_comments(self, number):
        return [dict(c) for c in self.thread] if number == 7 else super().issue_comments(number)


class ReviewDecisionsTests(unittest.TestCase):
    """The reviewers read the decisions of the operator on the issue (#472)."""

    def prompt(self, thread, hist=None):
        runner = FakeRunner(raw_review())
        d = Dispatcher(CFG, DecisionGH(thread, hist), FakeWS(), runner, log=lambda m: None)
        d._do_review(pr())
        return runner.calls[0][3]

    def test_the_review_gets_the_approval_of_the_operator(self):
        supervisor = comment(at='2026-11-19T11:10:00Z', body=HEADER + 'Approvato.')
        prompt = self.prompt([comment(), supervisor])
        section = prompt[prompt.index(DECISIONS):prompt.index('## Pull request description')]
        self.assertIn(f'### Comment by {OPERATOR}, {APPROVED}\n\n{APPROVAL}', section)
        self.assertNotIn('Approvato.', section)
        self.assertLess(prompt.index('## Trust policy'), prompt.index(DECISIONS))

    def test_an_edit_after_the_approval_takes_it_away(self):
        prompt = self.prompt([comment()], history(edited='2026-11-19T11:30:00Z'))
        self.assertIn(f'{DECISIONS}\n\n{NONE}', prompt)
        self.assertNotIn(APPROVAL_TEXT, prompt)

    def test_the_supervisor_identity_never_decides(self):
        # Its app among the trusted users, as a misconfiguration might put it: still no decision.
        cfg = dataclasses.replace(CFG, trusted_users=(*CFG.trusted_users, SUPERVISOR_LOGIN))

        class Identities:
            def login(self, actor):
                return SUPERVISOR_LOGIN if actor == cfg.app_of(cfg.supervisor) else None

            def agent_logins(self):
                return {SUPERVISOR_LOGIN}

            def github(self, actor, permissions=None):
                return gh

        cfg = dataclasses.replace(cfg, supervisor='agy')
        gh = DecisionGH([comment(login=SUPERVISOR_LOGIN)])
        d = Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                       identities=Identities())
        self.assertEqual(d._supervisor_login(), SUPERVISOR_LOGIN)
        issue, _ = d._issue_context(7)
        self.assertEqual(issue['decisions'], [])
        gh.thread.append(comment(at='2026-11-19T11:30:00Z'))
        issue, _ = d._issue_context(7)
        self.assertEqual([c['author']['login'] for c in issue['decisions']], [OPERATOR])


class PreflightGH(IssueGH):
    """Issue #7 waiting for its readiness check, with comments that carry their time."""

    def __init__(self, thread=(), hist=None):
        super().__init__()
        self.thread.extend(thread)
        self.hist = hist or history(taken=None)

    def issue_history(self, number):
        return dict(self.hist)

    def issue_comments(self, number):
        # Each comment with its id, as GitHub gives it: by its position, since the thread only
        # grows.
        return [dict(c, id=c.get('id', 1000 + i))
                for i, c in enumerate(super().issue_comments(number))]

    def say(self, body, who=OPERATOR):
        self.thread.append({'author': {'login': who}, 'body': body, 'created_at': LATER})


class PreflightTrustTests(unittest.TestCase):
    """The readiness check blocks an issue that touches the trust policy without a decision."""

    REASON = 'Punto 4: aggiunge `reviewer_decision` a `NON_CRITICAL`; nessuna decisione lo nomina.'

    def tick(self, output, thread=(), hist=None, cfg=PCFG):
        self.gh = PreflightGH(thread, hist)
        self.runner = FakeRunner(output)
        self.d = Dispatcher(cfg, self.gh, FakeWS(), self.runner, log=lambda m: None)
        self.assertEqual([a.kind for a in self.d.tick()], ['preflight'])
        return self.runner.calls[0][3]

    def planned(self):
        return [(a.kind, a.number) for a in plan(self.d.snapshot(), PCFG)]

    def record(self):
        return [r for c in self.gh.thread if (r := markdown.read_marker(c['body'], 'preflight'))][-1]

    def test_an_uncovered_change_blocks_with_the_question_to_the_operator(self):
        auto = dataclasses.replace(PCFG, merge=MergePolicy(mode='auto'), supervisor='agy')
        for cfg in (PCFG, auto):
            with self.subTest(mode=cfg.merge.mode):
                # Even when the agent says ready and lists no gap.
                prompt = self.tick(checked(True, trust='uncovered', reason=self.REASON), cfg=cfg)
                self.assertIn(trust_policy_rule(), prompt)
                self.assertIn(f'{DECISIONS}\n\n{NONE}', prompt)
                # To the operator, never to the supervisor.
                self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
                record = self.record()
                self.assertEqual((record['ready'], record['blocked']), (False, True))
                self.assertEqual(record['trust_policy'], 'uncovered')
                self.assertEqual(record['class'], 'operator_decision')
                first = record['gaps'][0]
                self.assertEqual(first['kind'], 'operator_decision')
                self.assertEqual(first['description'], f'{TRUST_QUESTION} {self.REASON}')
                body = self.gh.thread[-1]['body']
                self.assertIn('issue non pronta', body)
                self.assertIn('Approvi la modifica della policy di fiducia descritta qui?', body)
                self.assertIn(self.REASON, body)

    def test_the_question_comes_first_among_the_gaps(self):
        outcome = normalize_preflight(checked(False, [gap('default_available', 'JSON')],
                                              trust='uncovered', reason=''),
                                      language=CFG.language)
        self.assertFalse(outcome['ready'])
        self.assertEqual([g['kind'] for g in outcome['gaps']],
                         ['operator_decision', 'default_available'])
        self.assertEqual(outcome['gaps'][0]['description'], TRUST_QUESTION)
        # A default alone would go to the supervisor: with the question it is the operator's.
        self.assertEqual(preflight_class(outcome), 'operator_decision')
        for trust in ('none', 'covered'):
            with self.subTest(trust=trust):
                outcome = normalize_preflight(checked(True, trust=trust, reason='x'))
                self.assertTrue(outcome['ready'])
                self.assertEqual(outcome['gaps'], [])
                self.assertEqual(outcome['trust_policy'], trust)
        # An output written before #472 is read as before.
        legacy = {'ready': True, 'gaps': [], 'split': ''}
        self.assertEqual(normalize_preflight(legacy), legacy)

    def test_an_approval_of_the_operator_covers_it(self):
        reason = f'Punto 4: coperta dal commento di {OPERATOR} del {APPROVED}.'
        prompt = self.tick(checked(True, trust='covered', reason=reason), [comment()])
        section = prompt[prompt.index(DECISIONS):]
        self.assertIn(f'### Comment by {OPERATOR}, {APPROVED}\n\n{APPROVAL}', section)
        self.assertEqual(self.gh.labels[7], {READY})
        record = self.record()
        self.assertTrue(record['ready'])
        self.assertEqual((record['trust_policy'], record['trust_policy_reason']),
                         ('covered', reason))
        self.assertIn('**Policy di fiducia:** toccata, coperta da una decisione '
                      f'dell\'operatore. {reason}', self.gh.thread[-1]['body'])

    def test_a_check_that_relied_on_a_decision_no_longer_valid_runs_again(self):
        # Covered by the approval, then the text is changed and restored: title, body and
        # comments are the same, but the approval no longer counts. The check is not reused.
        reason = f'Punto 4: coperta dal commento di {OPERATOR} del {APPROVED}.'
        self.tick(checked(True, trust='covered', reason=reason), [comment()])
        self.assertEqual(self.planned(), [('implement', 7)])
        before = self.d._preflight_view(7, ())['hash']
        self.gh.hist = history(edited='2026-11-19T11:30:00Z', taken=None)
        self.assertEqual(self.d._issue_decisions(self.gh.thread, self.gh.hist), [])
        self.assertNotEqual(self.d._preflight_view(7, ())['hash'], before)
        self.assertEqual(self.planned(), [('preflight', 7)])
        # The new check reads no decision, and blocks the issue.
        self.runner.outputs.append(checked(True, trust='uncovered'))
        self.assertEqual([a.kind for a in self.d.tick()], ['preflight'])
        self.assertIn(f'{DECISIONS}\n\n{NONE}', self.runner.calls[-1][3])
        self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})
        # A new approval of the operator, after the change, makes it ready again.
        self.gh.thread.append(comment(at='2026-11-19T11:45:00Z'))
        self.gh.say('/devloop recheck')
        self.assertEqual(self.planned(), [('preflight', 7)])

    def test_without_decisions_the_hash_is_the_one_of_before(self):
        # Only the comments that may record a decision enter the hash: an issue without them
        # keeps the hash of its checks written before #472.
        d = Dispatcher(PCFG, PreflightGH(), FakeWS(), FakeRunner(), log=lambda m: None)
        hist, plain = history(taken=None), comment(at='2026-11-19T09:30:00Z')
        self.assertEqual(d._request_hash(hist, [plain]),
                         preflight_hash(hist['title'], hist['body'], [plain],
                                        PCFG.trusted_users, d._automatic, d._accepted))
        self.assertNotEqual(d._request_hash(hist, [comment()]),
                            preflight_hash(hist['title'], hist['body'], [comment()],
                                           PCFG.trusted_users, d._automatic, d._accepted))

    def test_an_approval_before_an_edit_or_from_the_supervisor_is_not_listed(self):
        for thread, hist in (([comment()], history(edited='2026-11-19T11:30:00Z', taken=None)),
                             ([comment(body=HEADER + APPROVAL)], None),
                             ([comment(login=SUPERVISOR_LOGIN)], None)):
            with self.subTest(thread=thread, hist=hist):
                prompt = self.tick(checked(True, trust='uncovered'), thread, hist)
                self.assertIn(f'{DECISIONS}\n\n{NONE}', prompt)
                self.assertEqual(self.gh.labels[7], {READY, NEEDS_OPERATOR})

    def test_the_schema_and_the_record_carry_the_declaration(self):
        s = schema.load_schema('preflight')
        self.assertEqual(s['properties']['trust_policy']['enum'], list(markdown.PREFLIGHT_TRUST))
        self.assertIn('trust_policy', s['required'])
        self.assertIn('trust_policy_reason', s['required'])
        record = dict(normalize_preflight(checked(False, trust='uncovered')), hash='h',
                      agent=REV, blocked=True, labeled=0)
        body = markdown.render_preflight(record, markdown.dispatcher_header())
        self.assertEqual(markdown.read_marker(body, 'preflight'), record)
        for broken in (dict(record, trust_policy='maybe'), dict(record, trust_policy_reason=1),
                       dict(record, ready=True, blocked=False)):
            with self.subTest(broken=broken):
                self.assertIsNone(markdown.read_marker(markdown.marker('preflight', broken),
                                                       'preflight'))


class FaithfulMergeTests(Case):
    """The merge of a PR that touches the trust policy (#472)."""

    def test_a_class_a_decision_names_is_merged_like_any_other_pr(self):
        # The reviewers judged it a faithful implementation (ADR 0005, D2 and P5-1): they
        # approve, and devloop merges the PR, which adds a class to NON_CRITICAL. The level is
        # not what this test is about: with `devloop/` among the sensitive paths, as in ssdlc,
        # the PR is merged the same way at level 2 once its two approvals are in.
        self.assertIn('round_grantable', NON_CRITICAL)
        files = ('devloop/supervision.py', 'tests/test_devloop_supervision.py',
                 'docs/devloop/guide.md')
        cfg = dataclasses.replace(STRICT, review_policy=dataclasses.replace(
            STRICT.review_policy, sensitive_paths=(), sensitive_files=()))
        approval = norm(raw_review(), bound=False)
        approval['receipt'] = receipt(files=files, cfg=cfg)
        gh = MergeGH()
        gh.thread = []
        gh.say(OPERATOR, markdown.render_review(approval, header(REV_NAME)))
        ws = FakeWS()
        ws.diff = changes(*files)
        d = self.make(cfg, gh=gh, ws=ws)
        self.assertEqual(d.snapshot().prs[0].merge_mode, 'strict')
        self.assertEqual(d.tick(), [MERGE])
        self.assertEqual(self.gh.merged, [(100, SHA1)], self.logs)
        self.assertNotIn(OPERATOR_LABEL, self.gh.labels[100])
        self.assertEqual(records(self.gh, 'notice'), [])

    def test_a_change_of_the_rule_goes_to_the_operator(self):
        # A reviewer gives trust_policy to a PR that edits the "Trust policy" section, which an
        # ADR asks for: the merge is the operator's.
        ws = FakeWS()
        ws.diff = changes('devloop/prompts/review.md', 'devloop/prompts/advisory.md')
        d = self.make(STRICT, gh=TrustGH(), ws=ws)
        self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
        self.assertEqual([a.kind for a in d.tick()], ['operator_merge'])
        self.assert_not_merged()
        self.assertIn(OPERATOR_LABEL, self.gh.labels[100])
        self.assertEqual(len(records(self.gh, 'notice')), 1)

    def test_operator_only_paths_stay_with_the_operator(self):
        # The ADR that records the decision is the operator's to merge, approved or not.
        ws = FakeWS()
        ws.diff = changes('devloop/supervision.py', 'docs/adr/0005-deleghe.md')
        d = self.make(STRICT, gh=MergeGH(), ws=ws)
        self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
        self.assert_not_merged()


BOT = 'other-app[bot]'


class ApproveCommandTests(unittest.TestCase):
    """Only a ``/devloop approve`` of a trusted user in person is a decision (#483)."""

    def test_an_approval_without_the_command_does_not_count(self):
        plain = comment(body=APPROVAL_TEXT)
        command = comment(at='2026-11-19T11:10:00Z')
        for taken in (False, True):
            with self.subTest(review=taken):
                self.assertEqual(decided([plain], taken=taken), [])
                self.assertEqual(decided([plain, command], taken=taken), [command])
        # The text may start on the line of the command.
        inline = comment(body=f'{APPROVE_COMMAND} {APPROVAL_TEXT}')
        self.assertEqual(decided([inline]), [inline])

    def test_the_command_is_the_first_line_followed_by_the_decision(self):
        self.assertEqual(approve_command(APPROVAL), APPROVAL_TEXT)
        self.assertEqual(approve_command(f'{APPROVE_COMMAND} il punto 1\nnon il 2'),
                         'il punto 1\nnon il 2')
        self.assertEqual(approve_command(HEADER + APPROVAL, after_header=True), APPROVAL_TEXT)
        for body in (APPROVE_COMMAND, f'{APPROVE_COMMAND}\n\n', f'Premessa.\n{APPROVAL}',
                     f'> citazione\n{APPROVAL}', HEADER + APPROVAL,
                     f'/devloop approved\n{APPROVAL_TEXT}', f'/devloop recheck\n{APPROVAL_TEXT}'):
            with self.subTest(body=body):
                self.assertFalse(approve_command(body))
                self.assertEqual(decided([comment(body=body)]), [])

    def test_an_app_or_the_supervisor_is_refused(self):
        # Even with their login among the trusted users, with or without the header.
        apps = (SUPERVISOR_LOGIN, 'claude-agent[bot]')
        trusted = {OPERATOR, *apps, BOT}
        for login in (*apps, BOT):
            for body in (APPROVAL, HEADER + APPROVAL):
                with self.subTest(login=login, body=body):
                    command = comment(login=login, body=body)
                    self.assertEqual(decided([command], trusted=trusted, apps=apps), [])
                    (refused, reason), = refused_approvals([command], trusted, set(apps),
                                                            language='Italian')
                    self.assertIs(refused, command)
                    self.assertIn('l\'autore è il supervisore o un\'app', reason)
        # Any app, trusted or not.
        self.assertEqual(len(refused_approvals([comment(login=BOT)], {OPERATOR}, set())), 1)

    def test_the_refusals_of_a_trusted_login_say_why(self):
        cases = ((HEADER + APPROVAL, 'il commento porta l\'intestazione degli agent'),
                 (f'> citazione\n{APPROVAL}', 'il comando non è la prima riga del commento'),
                 (APPROVE_COMMAND, 'manca il testo della decisione dopo il comando'))
        for body, reason in cases:
            with self.subTest(body=body):
                command = comment(body=body)
                (refused, why), = refused_approvals([command], {OPERATOR}, set(),
                                                    language='Italian')
                self.assertIs(refused, command)
                self.assertTrue(why.startswith(reason), why)
        # A command that counts, an approval without it and an untrusted author: no answer.
        self.assertEqual(refused_approvals([comment(), comment(body=APPROVAL_TEXT),
                                            comment(login='stranger')], {OPERATOR}, set()), [])


class RefusedAnswerTests(unittest.TestCase):
    """The readiness check answers the ``/devloop approve`` it refuses (#483)."""

    def test_the_check_says_why_a_command_does_not_count(self):
        bot = comment(login=BOT)
        header = comment(at='2026-11-19T11:10:00Z', body=HEADER + APPROVAL)
        plain = comment(at='2026-11-19T11:20:00Z', body=APPROVAL_TEXT)
        gh = PreflightGH([bot, header, plain], history(taken=None))
        runner = FakeRunner(checked(True, trust='uncovered'))
        logs = []
        d = Dispatcher(PCFG, gh, FakeWS(), runner, log=logs.append)
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        # None of them is a decision: the issue goes to the operator.
        self.assertIn(f'{DECISIONS}\n\n{NONE}', runner.calls[0][3])
        self.assertEqual(gh.labels[7], {READY, NEEDS_OPERATOR})
        body = gh.thread[-1]['body']
        self.assertIn('### `/devloop approve` rifiutati', body)
        self.assertIn(f'- il commento di `{BOT}` del {APPROVED}: l\'autore è il supervisore o '
                      'un\'app', body)
        self.assertIn(f'- il commento di `{OPERATOR}` del 2026-11-19T11:10:00Z: il commento '
                      'porta l\'intestazione degli agent', body)
        self.assertEqual(body.count('- il commento di '), 2)
        self.assertTrue(any(f'refused `{APPROVE_COMMAND}` by {BOT}' in line for line in logs))
        # The record names the comments it answered, by their ids.
        record = [r for c in gh.thread if (r := markdown.read_marker(c['body'], 'preflight'))]
        self.assertEqual(record[-1]['refused'], [1000, 1001])
        # Nothing else answers them, nor does a new check.
        self.assertEqual(self.planned(d), [])
        gh.say('/devloop recheck')
        runner.outputs.append(checked(True, trust='uncovered'))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertNotIn('rifiutati', gh.thread[-1]['body'])
        self.assertIn('issue non pronta', gh.thread[-1]['body'])

    @staticmethod
    def planned(d):
        return [(a.kind, a.number) for a in plan(d.snapshot(), PCFG)]

    def test_a_command_after_the_check_is_answered_once_without_a_new_check(self):
        # The case of the review of #484: a check concluded, then an app writes the command.
        # It changes neither the hash nor anything else, so no check runs to answer it.
        gh = PreflightGH(hist=history(taken=None))
        runner = FakeRunner(checked(True, trust='uncovered'))
        logs = []
        d = Dispatcher(PCFG, gh, FakeWS(), runner, log=logs.append)
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        digest = d._preflight_view(7, ())['hash']
        # The command of a trusted login after the header is part of the request instead: its
        # check runs again, and answers it.
        why = 'l\'autore è il supervisore o un\'app'
        for login in (BOT, SUPERVISOR_LOGIN):
            with self.subTest(login=login):
                gh.thread.append(comment(at=LATER, login=login))
                self.assertEqual(d._preflight_view(7, ())['hash'], digest)
                self.assertEqual(self.planned(d), [('approve_refused', 7)])
                del logs[:]
                self.assertEqual([a.kind for a in d.tick()], ['approve_refused'])
                answer = gh.thread[-1]
                self.assertEqual(markdown.read_marker(answer['body'], 'approve'),
                                 {'comments': [len(gh.thread) - 2 + 1000]})
                self.assertIn('## `/devloop approve` rifiutati', answer['body'])
                self.assertIn(f'- il commento di `{login}` del {LATER}: {why}', answer['body'])
                self.assertTrue(any(f'refused `{APPROVE_COMMAND}` by {login}' in line
                                    for line in logs))
                # Answered once: the next ticks do nothing, and the block stays the check's.
                self.assertEqual([a.kind for a in d.tick()], [])
                self.assertEqual([a.kind for a in d.tick()], [])
                self.assertEqual(gh.thread[-1], answer)
                self.assertEqual(gh.labels[7], {READY, NEEDS_OPERATOR})
                self.assertTrue(d._preflight_view(7, gh.labels[7])['owned'])
        # The answer spends no `/devloop recheck`: one before it still runs the check, which
        # answers the commands not answered yet, and only those.
        gh.say('/devloop recheck')
        gh.thread.append(comment(at=LATER, login=BOT))
        self.assertEqual(d._preflight_view(7, ())['approvals'], 1)
        self.assertEqual(self.planned(d), [('preflight', 7)])
        runner.outputs.append(checked(True, trust='uncovered'))
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        body = gh.thread[-1]['body']
        self.assertEqual(body.count('- il commento di '), 1)
        self.assertEqual(markdown.read_marker(body, 'preflight')['refused'],
                         [len(gh.thread) - 2 + 1000])
        self.assertEqual(self.planned(d), [])

    def test_the_command_of_the_operator_answers_the_question(self):
        gh = PreflightGH(hist=history(taken=None))
        runner = FakeRunner(checked(True, trust='uncovered'))
        d = Dispatcher(PCFG, gh, FakeWS(), runner, log=lambda m: None)
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertEqual(gh.labels[7], {READY, NEEDS_OPERATOR})
        # A plain approval is part of the request, but no decision; the command is one, with
        # its text in the hash.
        gh.thread.append(comment(at=LATER, body=APPROVAL_TEXT))
        self.assertEqual(d._issue_decisions(gh.thread, gh.hist), [])
        before = d._preflight_view(7, ())['hash']
        gh.thread.append(comment(at='2026-11-19T13:30:00Z'))
        self.assertEqual(len(d._issue_decisions(gh.thread, gh.hist)), 1)
        approved = d._preflight_view(7, ())['hash']
        self.assertNotEqual(approved, before)
        gh.thread[-1] = comment(at='2026-11-19T13:30:00Z', body=f'{APPROVE_COMMAND}\nSolo il 1.')
        self.assertNotIn(d._preflight_view(7, ())['hash'], (before, approved))
        self.assertEqual([(a.kind, a.number) for a in plan(d.snapshot(), PCFG)],
                         [('preflight', 7)])


class CompleteHistoryTests(unittest.TestCase):
    """The history of an issue is read in full, or no decision counts (#483)."""

    def test_an_incomplete_history_admits_no_decision(self):
        missing = {k: v for k, v in history().items() if k != 'complete'}
        for hist in (history(complete=False), missing):
            for taken in (False, True):
                with self.subTest(hist=hist, review=taken):
                    self.assertEqual(decided([comment()], hist, taken=taken), [])
        self.assertEqual(decided([comment()], history()), [comment()])
        # The reviewers and the readiness check get none.
        d = Dispatcher(CFG, DecisionGH([comment()], history(complete=False)), FakeWS(),
                       FakeRunner(), log=lambda m: None)
        self.assertEqual(d._issue_context(7)[0]['decisions'], [])
        gh = PreflightGH([comment()], history(taken=None, complete=False))
        runner = FakeRunner(checked(True, trust='uncovered'))
        d = Dispatcher(PCFG, gh, FakeWS(), runner, log=lambda m: None)
        self.assertEqual([a.kind for a in d.tick()], ['preflight'])
        self.assertIn(f'{DECISIONS}\n\n{NONE}', runner.calls[0][3])

    @staticmethod
    def page(nodes, more, cursor):
        info = {} if more is None else {'hasNextPage': more, 'endCursor': cursor}
        return json.dumps({'data': {'repository': {'issue': {
            'title': 'Issue 7', 'body': 'Do the thing.', 'author': {'login': OPERATOR},
            'lastEditedAt': EDITED, 'editor': {'login': OPERATOR},
            'timelineItems': {'pageInfo': info, 'nodes': nodes}}}}})

    @staticmethod
    def labeled(label, at):
        return {'__typename': 'LabeledEvent', 'createdAt': at, 'actor': {'login': OPERATOR},
                'label': {'name': label}}

    def github(self, pages):
        """A ``GitHub`` whose history is ``pages``, keyed by the cursor that asks for each."""
        self.calls = []

        def run(argv, **kw):
            after = next((a[len('after='):] for a in argv if a.startswith('after=')), None)
            self.calls.append(after)
            return SimpleNamespace(returncode=0, stdout=pages[after], stderr='')
        return GitHub('o/r', run=run)

    def test_every_page_is_read(self):
        # The takeover is on the first page, after 100 events; a later one on the second. With
        # only the latest page the bound would move to the later takeover.
        first = ([self.labeled('agent:ready', CREATED)] * 99
                 + [self.labeled(WORKING, TAKEN)])
        second = [self.labeled(WORKING, '2026-11-19T14:00:00Z'),
                  {'__typename': 'RenamedTitleEvent', 'createdAt': '2026-11-19T09:30:00Z',
                   'actor': {'login': OPERATOR}}]
        gh = self.github({None: self.page(first, True, 'c1'),
                          'c1': self.page(second, False, 'c2')})
        hist = gh.issue_history(7)
        self.assertEqual(self.calls, [None, 'c1'])
        self.assertTrue(hist['complete'])
        self.assertEqual(len(hist['labeled']), 101)
        self.assertEqual(hist['labeled'][99], (WORKING, OPERATOR, TAKEN))
        self.assertEqual(hist['renamed'], [(OPERATOR, '2026-11-19T09:30:00Z')])
        # A command written after the first takeover does not count.
        late = comment(at=LATER)
        self.assertEqual(decided([comment(), late], hist, taken=True), [comment()])

    def test_a_read_that_stops_before_the_last_page_is_incomplete(self):
        node = [self.labeled(WORKING, TAKEN)]
        for pages in ({None: self.page(node, True, None)},
                      {None: self.page(node, True, '')},
                      {None: self.page(node, None, None)}):
            with self.subTest(pages=pages):
                hist = self.github(pages).issue_history(7)
                self.assertFalse(hist['complete'])
                self.assertEqual(decided([comment()], hist), [])
        # More pages than the read allows.
        endless = {None: self.page(node, True, 'c1'), 'c1': self.page(node, True, 'c2'),
                   'c2': self.page(node, True, 'c3')}
        with mock.patch('devloop.github._HISTORY_PAGES', 3):
            hist = self.github(endless).issue_history(7)
        self.assertEqual(self.calls, [None, 'c1', 'c2'])
        self.assertFalse(hist['complete'])


def _flat(text):
    return ' '.join(text.split())


RESERVED = ('stay with the operator even when a decision of the operator, a `/devloop approve` on '
            'the issue included, prescribes the change (#483): the quorum and the choice of the '
            'reviewers; the checks of `operator_only_paths` and of `protected_paths`; the names '
            'of the labels of hold, of escalation and of merge; `trusted_users`; this "Trust '
            'policy" section itself.')
QUORUM_EXAMPLE = ('Change of the rule, the operator is needed (point 3): the issue carries a '
                  '`/devloop approve` of the operator that names the change, and the change '
                  'lowers the quorum of a level, from two approvals to one, or narrows the check '
                  'of `operator_only_paths` so that it no longer covers `docs/adr/`. The approval '
                  'does not make it a faithful implementation.')


class ReservedToTheOperatorTests(unittest.TestCase):
    """What stays with the operator whatever a decision on the issue says (#483)."""

    def test_the_prompts_list_it_with_an_example(self):
        runner = FakeRunner(raw_review())
        d = Dispatcher(CFG, DecisionGH([comment()]), FakeWS(), runner, log=lambda m: None)
        d._do_review(pr())
        review = _flat(runner.calls[0][3])
        advisory = _flat((PROMPT_DIR / 'advisory.md').read_text(encoding='utf-8'))
        for name, text in (('review', review), ('advisory', advisory),
                           ('preflight', _flat(trust_policy_rule()))):
            with self.subTest(prompt=name):
                self.assertIn(RESERVED, text)
                self.assertIn(QUORUM_EXAMPLE, text)
                self.assertIn('What point 3 lists is never a faithful implementation: a change '
                              'of the quorum, for one, is a condition of the merge that still '
                              'needs the operator.', text)
                self.assertIn('an approval written without the command included, is not a '
                              'decision', text)
        # The review gets the approval all the same: the exclusion holds with it.
        self.assertIn(_flat(f'### Comment by {OPERATOR}, {APPROVED}\n\n{APPROVAL}'), review)


class ApprovedTrustGH(TrustGH):
    """``TrustGH`` whose issue #7 carries a ``/devloop approve`` of the operator, written
    before the takeover, that names the change."""

    APPROVAL = (f'{APPROVE_COMMAND}\nApprovo di abbassare il quorum del livello 2 a una sola '
                'approvazione e di togliere `docs/adr/` da `operator_only_paths`.')

    def __init__(self, raw):
        super().__init__(raw)
        self.decision = comment(body=self.APPROVAL)

    def issue_history(self, number):
        return history()

    def issue_comments(self, number):
        return [dict(self.decision)] if number == 7 else super().issue_comments(number)


class ReservedMergeTests(Case):
    """A PR that changes what stays with the operator goes to the operator for the merge, with
    a decision on its issue too (#483)."""

    def test_the_quorum_or_the_operator_only_paths_check_go_to_the_operator(self):
        parts = {'devloop/policy.py': 'Punto 3: abbassa il quorum del livello 2 '
                                      '(`devloop/policy.py`); riservato all\'operatore anche con '
                                      'la decisione sulla issue.',
                 'devloop/dispatch_merge.py': 'Punto 3: il controllo di `operator_only_paths` '
                                              'non copre più `docs/adr/` '
                                              '(`devloop/dispatch_merge.py`); riservato '
                                              'all\'operatore anche con la decisione sulla issue.'}
        auto = dataclasses.replace(STRICT, merge=MergePolicy(mode='auto'), supervisor='agy')
        for path, reason in parts.items():
            for cfg in (STRICT, auto):
                with self.subTest(path=path, mode=cfg.merge.mode):
                    ws = FakeWS()
                    ws.diff = changes(path, 'tests/test_devloop_merge.py')
                    gh = ApprovedTrustGH(trust(human=reason))
                    d = self.make(cfg, gh=gh, ws=ws)
                    # The command is a decision the reviewers read.
                    self.assertEqual(d._issue_context(7)[0]['decisions'], [gh.decision])
                    self.assertEqual(d.snapshot().prs[0].merge_mode, 'off')
                    self.assertEqual([a.kind for a in d.tick()], ['operator_merge'])
                    self.assert_not_merged()
                    self.assertIn(OPERATOR_LABEL, self.gh.labels[100])
                    body = self.gh.comments[-1][1]
                    self.assertIn('Il merge spetta all\'operatore', body)
                    self.assertIn(reason, body)
                    self.assertEqual(len(records(self.gh, 'notice')), 1)


if __name__ == '__main__':
    unittest.main()
