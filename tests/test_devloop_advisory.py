"""devloop: the advisory review of a PR in agent:needs-operator (#369).

The agents are simulated (``FakeRunner``): the tests check when the advisory review runs, how it
is published and that it changes nothing devloop decides on."""

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from devloop import config, markdown, receipt  # noqa: E402
from devloop.agents import AgentError, AgentRunner  # noqa: E402
from devloop.dispatch_advisory import (MAX_ATTEMPTS, RETRY_SECONDS,  # noqa: E402
                                       TEXTS, AdvisoryFailures, AdvisoryResults,
                                       normalize_advisory)
from devloop.dispatcher import Dispatcher  # noqa: E402
from devloop.github import GitHubError  # noqa: E402
from devloop.ledger import Ledger  # noqa: E402
from devloop.planning import counted_rounds, plan  # noqa: E402
from devloop.workspace import Workspace  # noqa: E402
from helpers import ssdlc_project  # noqa: E402
from helpers.devloop_fakes import SHA1, FakeIdentities  # noqa: E402
from test_devloop import CFG, IMPL, META, REV, FakeRunner, FakeWS, raw_review  # noqa: E402

# The title of the advisory review in the language of the test project (#632).
ADVISORY_TITLE = TEXTS.texts(CFG.language)('title')
from test_devloop import receipt as policy_receipt  # noqa: E402
from test_devloop_conclave import ThreadGH  # noqa: E402
from test_devloop_receipt import ReceiptGH, receipts  # noqa: E402

NEEDS_OPERATOR = 'agent:needs-operator'
READY = 'agent:ready-to-merge'
SHA2 = 'b' * 40
LOGINS = FakeIdentities.LOGINS
# The key on, with the implementer as the advisory agent: the case the issue asks for.
ON = dataclasses.replace(CFG, review_policy=dataclasses.replace(CFG.review_policy,
                                                                advisory_reviewer=IMPL))


def answer(opinion='dubbi'):
    return {'summary': 'Aggiunge la chiave X.', 'residual_risks': ['La chiave non è validata.'],
            'operator_checks': ['Il default in config.toml.'], 'overlooked': [],
            'unfounded': [], 'opinion': opinion, 'opinion_reason': 'Manca un test del default.'}


class AdvisoryWS(FakeWS):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.copies = []

    def commit_copy(self, name, commit, branch=None):
        self.copies.append((name, commit, branch))
        return Path('/wt') / name


class OperatorGH(ThreadGH):
    """ThreadGH with PR #100 of issue 7 open at ``head``, opened by the implementer's app."""

    def __init__(self, head=SHA1):
        super().__init__(head=head)
        self.review_reads = 0
        self.state = 'OPEN'

    def pr_state(self, number):
        return dict(super().pr_state(number), state=self.state)

    def open_prs(self):
        return [{'number': 100, 'title': 't', 'body': markdown.publishable('b', 'pr', META),
                 'labels': [{'name': n} for n in sorted(self.labels.get(100, ()))],
                 'headRefName': 'agent/issue-7', 'headRefOid': self.head,
                 'author': {'login': LOGINS[IMPL]}, 'isCrossRepository': False,
                 'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN'}]

    def pr_reviews(self, number):
        self.review_reads += 1
        return super().pr_reviews(number)


def records(gh, kind, number=100):
    return [(c, r) for c in gh.thread.get(number, [])
            if (r := markdown.read_marker(c['body'], kind)) is not None]


def no_summary(gh):
    """Make the dispatcher's advisory summary fail on ``gh`` (HTTP 502); returns the method to
    put back."""
    comment = gh.comment

    def broken(number, body):
        if markdown.read_marker(body, 'advisory') is not None:
            raise GitHubError('HTTP 502')
        return comment(number, body)
    gh.comment = broken
    return comment


def advisory_reviews(gh):
    return [r for _, r in gh.native if markdown.read_marker(r['body'], 'advisory')]


def advisory_turns(runner):
    return [call for call in runner.calls if call[2] == 'advisory']


class Base(unittest.TestCase):
    def make(self, *outputs, cfg=ON, labels=(NEEDS_OPERATOR,)):
        self.gh = OperatorGH()
        self.gh.labels[100] = set(labels)
        self.ws = AdvisoryWS()
        self.runner = FakeRunner(*outputs)
        self.logs = []
        self.d = Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                            identities=FakeIdentities(self.gh))
        self.d.review_retry_pause = 0
        return self.d

    def tick(self, cfg=ON):
        """Snapshot, plan and run the advisory actions; the other actions are returned."""
        snap = self.d.snapshot()
        prs = {p.number: p for p in snap.prs}
        actions = plan(snap, cfg)
        for action in actions:
            if action.kind == 'advisory':
                self.d._run_action(action, prs)
        return snap, actions


class ConfigTests(unittest.TestCase):
    def data(self, **policy):
        data = ssdlc_project.data()
        data['review_policy'] = dict(data['review_policy'], **policy)
        return data

    def test_off_by_default_and_out_of_the_policy_version(self):
        self.assertEqual(CFG.review_policy.advisory_reviewer, '')
        # Turning it on voids no approval: the policy version does not change.
        self.assertEqual(ON.review_policy.version(), CFG.review_policy.version())

    def test_an_agent_of_the_configuration_or_nothing(self):
        cfg = config.parse_config(self.data(advisory_reviewer='claude'))
        self.assertEqual(cfg.review_policy.advisory_reviewer, 'claude')
        for value in ('nobody', 1, ['claude']):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.parse_config(self.data(advisory_reviewer=value))
        # The local overlay turns it on.
        self.assertIn('advisory_reviewer', config.OVERLAY_KEYS['review_policy'])


class OffTests(Base):
    def test_with_the_key_off_nothing_changes(self):
        self.make(answer(), cfg=CFG)
        snap, actions = self.tick(cfg=CFG)
        self.assertEqual(snap.advisories, [])
        self.assertNotIn('advisory', [a.kind for a in actions])
        # Not even a read of the native reviews, no turn, nothing published.
        self.assertEqual(self.gh.review_reads, 0)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.native, [])
        self.assertEqual(self.gh.thread.get(100, []), [])
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        # An action left from an earlier configuration does nothing either.
        self.d._do_advisory(snap.prs[0])
        self.assertEqual(self.runner.calls, [])


class AdvisoryTests(Base):
    def test_one_comment_review_per_head_with_the_agents_identity(self):
        self.make(answer(), answer('ok'))
        snap, actions = self.tick()
        self.assertEqual(snap.advisories, [{'number': 100, 'head': SHA1}])
        self.assertEqual([(a.kind, a.number) for a in actions], [('advisory', 100)])
        # One read-only turn of the configured agent, on a disposable checkout of the head.
        ((agent, mode, schema, prompt),) = self.runner.calls
        self.assertEqual((agent, mode, schema), (IMPL, 'review', 'advisory'))
        self.assertIn(ADVISORY_TITLE, ' '.join(prompt.split()))
        self.assertIn('Do the thing.', prompt)
        self.assertEqual(self.ws.copies, [('pr-100-advisory', SHA1, 'agent/issue-7')])
        self.assertIn('pr-100-advisory', self.ws.removed)
        # One native review of type COMMENT, with the agent's identity, on the head.
        ((number, review),) = self.gh.native
        self.assertEqual((number, review['event'], review['commit'], review['login']),
                         (100, 'COMMENT', SHA1, LOGINS[IMPL]))
        self.assertIn(f'## {ADVISORY_TITLE}', review['body'])
        self.assertIn('La chiave non è validata.', review['body'])
        self.assertEqual(markdown.read_marker(review['body'], 'advisory'),
                         {'head': SHA1, 'reviewer': IMPL, 'outcome': 'done', 'opinion': 'dubbi'})
        # The dispatcher's summary for the operator: the opinion, never the agent's text.
        ((comment, record),) = records(self.gh, 'advisory')
        self.assertEqual(comment['author']['login'], LOGINS['dispatcher'])
        self.assertEqual(record['opinion'], 'dubbi')
        self.assertIn('**dubbi**', comment['body'])
        self.assertNotIn('La chiave non è validata.', comment['body'])
        # Once per head: the same head gets no second one.
        snap, actions = self.tick()
        self.assertEqual(snap.advisories, [])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.gh.native), 1)
        # A new head gets its own.
        self.gh.head = SHA2
        snap, _ = self.tick()
        self.assertEqual(snap.advisories, [{'number': 100, 'head': SHA2}])
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual([r['commit'] for _, r in self.gh.native], [SHA1, SHA2])

    def test_a_native_review_without_its_summary_gets_it_without_a_new_turn(self):
        self.make(answer(), answer('ok'))
        comment = no_summary(self.gh)
        self.tick()
        # The review is published, its summary failed: the head is not done yet.
        self.assertEqual(len(self.gh.native), 1)
        self.assertEqual(records(self.gh, 'advisory'), [])
        self.assertFalse(self.d._advisory_done(100, SHA1))
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        # Even without the kept result (a restart without state_dir), the native review stands
        # for the turn: the next tick writes the summary from it, without a new turn.
        self.gh.comment = comment
        self.d.advisory_results = AdvisoryResults()
        snap, _ = self.tick()
        self.assertEqual(snap.advisories, [{'number': 100, 'head': SHA1}])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.gh.native), 1)
        ((_, record),) = records(self.gh, 'advisory')
        self.assertEqual(record, {'head': SHA1, 'reviewer': IMPL, 'outcome': 'done',
                                  'opinion': 'dubbi'})
        snap, _ = self.tick()
        self.assertEqual(snap.advisories, [])
        self.assertEqual(len(self.runner.calls), 1)
        # Only the configured agent's identity counts.
        self.gh.native = [(n, dict(r, login=LOGINS[REV])) for n, r in self.gh.native]
        self.assertIsNone(self.d._advisory_published(100, SHA1, IMPL))

    def test_a_kept_result_is_published_without_a_new_turn(self):
        # The summary of a failed turn fails too: the next tick writes it, with no turn.
        self.make(AgentError('timeout after 600s'), answer())
        comment = no_summary(self.gh)
        self.tick()
        self.assertEqual(records(self.gh, 'advisory'), [])
        self.gh.comment = comment
        self.tick()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.native, [])
        ((comment, record),) = records(self.gh, 'advisory')
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('timeout after 600s', comment['body'])
        # Once written, the kept result is forgotten.
        self.assertIsNone(self.d.advisory_results.get(100, SHA1, IMPL))

    def test_no_round_no_finding_no_label_no_promotion(self):
        self.make(answer('contrario'), labels=(NEEDS_OPERATOR, READY))
        snap, _ = self.tick()
        target = snap.prs[0]
        thread = self.d._pr_thread(100)
        # Nothing a round, a finding or a promotion is read from.
        for kind in ('review', 'fix', 'finding', 'policy', 'notice', 'supervision'):
            with self.subTest(kind=kind):
                self.assertEqual(self.d._markers(target, thread, kind)
                                 if kind in ('review', 'fix', 'finding', 'policy')
                                 else self.d._dispatcher_records(thread, kind), [])
        self.d._read_thread(target, thread)
        self.assertEqual((target.reviews, target.closures, target.promotions), ([], [], 0))
        self.assertEqual(counted_rounds(target.reviews), 0)
        # The labels stay: no edit at all.
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR, READY})
        self.assertEqual(getattr(self.gh, 'label_log', {}), {})
        # Never an approval nor a request for changes.
        self.assertEqual([r['event'] for _, r in self.gh.native], ['COMMENT'])

    def test_only_a_pr_in_needs_operator_not_with_the_supervisor(self):
        for labels in (('agent:review',), (NEEDS_OPERATOR, 'agent:needs-supervisor')):
            with self.subTest(labels=labels):
                self.make(answer(), labels=labels)
                snap, actions = self.tick()
                self.assertEqual(snap.advisories, [])
                self.assertNotIn('advisory', [a.kind for a in actions])

    def test_a_failed_turn_is_recorded_once_and_never_escalated(self):
        self.make(AgentError('timeout after 600s'))
        self.tick()
        self.assertEqual(self.gh.native, [])
        ((comment, record),) = records(self.gh, 'advisory')
        self.assertEqual((record['outcome'], record['opinion']), ('failed', None))
        self.assertIn('non è riuscita', comment['body'])
        self.assertEqual(records(self.gh, 'notice'), [])
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        snap, _ = self.tick()
        self.assertEqual(snap.advisories, [])

    def test_a_github_failure_is_not_escalated_and_is_tried_again(self):
        self.make(answer(), answer())
        submit = self.gh.submit_review

        def broken(*args, **kw):
            raise GitHubError('HTTP 502')
        self.gh.submit_review = broken
        # However many ticks GitHub fails, the head gets one turn: only the publication is
        # tried again.
        for _ in range(3):
            self.tick()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.thread.get(100, []), [])
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        self.gh.submit_review = submit
        self.tick()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.gh.native), 1)
        self.assertEqual(len(records(self.gh, 'advisory')), 1)
        # The native review published later is the one the turn wrote.
        self.assertIn(ADVISORY_TITLE, self.gh.native[0][1]['body'])
        self.assertIn('La chiave non è validata.', self.gh.native[0][1]['body'])

    def during_the_turn(self, change):
        """Run ``change`` while the agent's turn runs."""
        run = self.runner.run

        def changing(*args):
            change()
            return run(*args)
        self.runner.run = changing

    def test_labels_or_head_changed_publish_nothing(self):
        self.make(answer())
        snap = self.d.snapshot()
        # Before the turn: labels changed, or the PR closed.
        self.gh.labels[100] = {'agent:review'}
        self.d._do_advisory(snap.prs[0])
        self.gh.labels[100] = {NEEDS_OPERATOR}
        self.gh.state = 'CLOSED'
        self.d._do_advisory(snap.prs[0])
        self.assertEqual(self.runner.calls, [])
        self.gh.state = 'OPEN'
        # The head moves during the turn: the review of the old head is not published.
        self.during_the_turn(lambda: setattr(self.gh, 'head', SHA2))
        self.d._do_advisory(snap.prs[0])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.native, [])
        self.assertEqual(records(self.gh, 'advisory'), [])

    def test_labels_changed_or_pr_closed_during_the_turn_publish_nothing(self):
        changes = {
            'supervisor': lambda gh: gh.labels[100].add('agent:needs-supervisor'),
            'review': lambda gh: gh.labels.__setitem__(100, {'agent:review'}),
            'closed': lambda gh: setattr(gh, 'state', 'CLOSED'),
            'merged': lambda gh: setattr(gh, 'state', 'MERGED'),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.make(answer(), answer())
                snap = self.d.snapshot()
                self.during_the_turn(lambda: change(self.gh))
                self.d._do_advisory(snap.prs[0])
                self.assertEqual(len(self.runner.calls), 1)
                self.assertEqual(self.gh.native, [])
                self.assertEqual(records(self.gh, 'advisory'), [])
        # Back to the operator on the same head, the kept result is published: no new turn.
        self.gh.labels[100] = {NEEDS_OPERATOR}
        self.gh.state = 'OPEN'
        self.tick()
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(len(self.gh.native), 1)
        self.assertEqual(len(records(self.gh, 'advisory')), 1)

    def test_the_kept_results_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as state:
            kept = {'head': SHA1, 'reviewer': IMPL, 'advisory': normalize_advisory(answer()),
                    'error': '', 'header': 'h'}
            AdvisoryResults(state).put(100, kept)
            results = AdvisoryResults(state)
            self.assertEqual(results.get(100, SHA1, IMPL), kept)
            # Another head, another agent: nothing kept for them.
            self.assertIsNone(results.get(100, SHA2, IMPL))
            self.assertIsNone(results.get(100, SHA1, REV))
            results.drop(100)
            self.assertIsNone(AdvisoryResults(state).get(100, SHA1, IMPL))
            # A file that is not valid counts as empty.
            (Path(state) / AdvisoryResults.FILE).write_text('{not json')
            self.assertIsNone(AdvisoryResults(state).get(100, SHA1, IMPL))
            AdvisoryResults(state).put(100, dict(kept, advisory={'opinion': 'forse'}))
            self.assertIsNone(AdvisoryResults(state).get(100, SHA1, IMPL))

    def test_the_answer_needs_a_valid_opinion(self):
        self.assertEqual(normalize_advisory(answer('ok'))['opinion'], 'ok')
        for raw in (dict(answer(), opinion='forse'), None):
            with self.subTest(raw=raw), self.assertRaises(AgentError):
                normalize_advisory(raw)


class ReceiptTests(unittest.TestCase):
    """The opinion in the receipt of the same head (#135)."""

    def test_the_current_receipt_of_the_head_gets_the_opinion(self):
        # A PR promoted, then given to the operator for its merge (#248, merge mode off).
        gh = ReceiptGH()
        gh.labels[100] = {'agent:review'}
        ledger = Ledger(gh)
        logs = []
        d = Dispatcher(ON, gh, AdvisoryWS(), FakeRunner(raw_review(), answer('ok')),
                       log=logs.append, ledger=ledger)
        d.tick()
        d.tick()
        self.assertEqual(gh.labels[100], {READY, NEEDS_OPERATOR})
        ((before, old),) = receipts(gh)
        before = before['body']
        policies = [r for _, r in records(gh, 'policy')]
        d.tick()
        ((after, new),) = receipts(gh)
        # Rewritten in place: digest and totals recomputed, promotion, state and policy kept.
        self.assertIn('### Review consultiva', after['body'])
        self.assertIn('**ok**', after['body'])
        self.assertNotEqual(new['digest'], old['digest'])
        self.assertEqual(new['totals']['turns'], old['totals']['turns'] + 1)
        for key in ('promotion', 'state', 'head', 'policy', 'level', 'base'):
            self.assertEqual(new[key], old[key])
        self.assertEqual([r for _, r in records(gh, 'policy')], policies)
        self.assertEqual(before.split('## Ricevuta della PR #100')[0],
                         after['body'].split('## Ricevuta della PR #100')[0])
        # The summary for the operator is there too, and the labels stay.
        self.assertEqual(len(records(gh, 'advisory')), 1)
        self.assertEqual(gh.labels[100], {READY, NEEDS_OPERATOR})

    def test_a_partial_publication_completes_receipt_and_summary_without_a_new_turn(self):
        gh = ReceiptGH()
        gh.labels[100] = {'agent:review'}
        runner = FakeRunner(raw_review(), answer('ok'), answer('contrario'))
        d = Dispatcher(ON, gh, AdvisoryWS(), runner, log=lambda m: None, ledger=Ledger(gh))
        d.tick()
        d.tick()
        ((before, old),) = receipts(gh)
        edit = gh.edit_comment

        def broken(comment_id, body):
            if comment_id == before['id']:
                raise GitHubError('HTTP 502')
            return edit(comment_id, body)
        # The receipt's edit fails after the native review: no summary yet.
        gh.edit_comment = broken
        d.tick()
        self.assertEqual(len(advisory_turns(runner)), 1)
        self.assertEqual(len(advisory_reviews(gh)), 1)
        self.assertEqual(records(gh, 'advisory'), [])
        self.assertEqual(receipts(gh)[0][1]['digest'], old['digest'])
        # Then the summary fails, after the receipt.
        gh.edit_comment = edit
        comment = no_summary(gh)
        d.tick()
        self.assertEqual(records(gh, 'advisory'), [])
        ((after, new),) = receipts(gh)
        self.assertIn('### Review consultiva', after['body'])
        self.assertNotEqual(new['digest'], old['digest'])
        # The next tick writes the summary: the receipt and the opinion stay those of the one
        # turn of the head.
        gh.comment = comment
        d.tick()
        self.assertEqual(len(advisory_turns(runner)), 1)
        self.assertEqual(len(advisory_reviews(gh)), 1)
        ((_, record),) = records(gh, 'advisory')
        self.assertEqual(record['opinion'], 'ok')
        ((after, last),) = receipts(gh)
        self.assertIn('**ok**', after['body'])
        self.assertEqual(last['digest'], new['digest'])
        self.assertEqual(gh.labels[100], {READY, NEEDS_OPERATOR})

    def test_a_later_receipt_of_the_same_head_carries_the_opinion(self):
        gh = ReceiptGH()
        d = Dispatcher(ON, gh, AdvisoryWS(), FakeRunner(), log=lambda m: None,
                       ledger=Ledger(gh))
        target = d.snapshot().prs[0]
        gh.comment(100, markdown.publishable(
            'riepilogo', 'advisory',
            {'head': SHA1, 'reviewer': IMPL, 'outcome': 'done', 'opinion': 'contrario'}))
        gh.comment(100, markdown.publishable(
            'riepilogo', 'advisory',
            {'head': SHA2, 'reviewer': IMPL, 'outcome': 'done', 'opinion': 'ok'}))
        coherent, (content, _) = d._checked_receipt(target, policy_receipt(), SHA1, None, [])
        self.assertTrue(coherent)
        self.assertEqual(content['advisory'], {'head': SHA1, 'reviewer': IMPL,
                                               'outcome': 'done', 'opinion': 'contrario'})
        self.assertIn('**contrario**', receipt.section(content))

    def test_without_an_advisory_review_the_receipt_is_the_one_before(self):
        args = dict(pr=100, issue=7, head=SHA1, promotion=1, policy=policy_receipt(),
                    request=None, issue_records={'turns': [], 'verifications': []},
                    pr_records={'turns': [], 'events': [], 'verifications': []}, reviews=[],
                    closures=[], approved_round=None, criteria=None, acceptance_map=None)
        content = receipt.build(**args)
        self.assertNotIn('advisory', content)
        self.assertNotIn('Review consultiva', receipt.section(content, 'Italian'))
        self.assertNotIn('Advisory review', receipt.section(content))
        self.assertEqual(receipt.digest(content),
                         receipt.digest(receipt.build(**args, advisory=None)))


def ledger_turns(gh, number=100, kind='advisory'):
    """``(comment, record)`` of the ledger's turns of ``kind`` on ``number``."""
    return [(c, r) for c, r in records(gh, 'ledger', number)
            if r.get('type') == 'turn' and r.get('kind') == kind]


def cli(advisory):
    """The ``run`` of a real ``AgentRunner``, as the agent CLIs answer: ``advisory`` for the
    turn in the advisory checkout, a review otherwise, in the envelope of each kind of CLI."""
    def run(argv, **kw):
        if argv[-1] == '--version':
            return SimpleNamespace(returncode=0, stdout='2.1.322 (Claude Code)', stderr='')
        out = advisory if str(kw['cwd']).endswith('pr-100-advisory') else raw_review()
        for arg in argv:  # codex writes its final message in a file
            if str(arg).endswith('.last.json'):
                Path(arg).write_text(json.dumps(out), encoding='utf-8')
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(
            {'is_error': False, 'status': 'SUCCESS', 'structured_output': out}))
    return run


class EndToEndTests(unittest.TestCase):
    """The advisory action as devloop runs it (#381): the real ``AgentRunner``, with its
    schema, and the ledger."""

    def test_the_advisory_action_runs_its_turn_closes_it_and_completes_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A PR promoted, then given to the operator for its merge (#248, merge mode off).
            gh = ReceiptGH()
            gh.labels[100] = {'agent:review'}
            logs = []
            runner = AgentRunner(Path(tmp) / 'logs', 10, run=cli(answer('ok')))
            d = Dispatcher(ON, gh, AdvisoryWS(), runner, log=logs.append, ledger=Ledger(gh))
            d.tick()
            d.tick()
            self.assertEqual(gh.labels[100], {READY, NEEDS_OPERATOR})
            self.assertEqual(ledger_turns(gh), [])
            d.tick()
            self.assertFalse([line for line in logs if 'unexpected error' in line], logs)
            # The turn is opened, the agent answers against the advisory schema, and the turn
            # is closed.
            ((_, turn),) = ledger_turns(gh)
            self.assertEqual((turn['state'], turn['outcome'], turn['agent'], turn['mode']),
                             ('closed', 'success', IMPL, 'review'))
            self.assertTrue(list((Path(tmp) / 'logs').glob(f'*-pr-100-advisory-{IMPL}.log')))
            # One native COMMENT review with the agent's opinion, the receipt of the head and
            # the summary for the operator.
            (review,) = advisory_reviews(gh)
            self.assertEqual(review['event'], 'COMMENT')
            self.assertEqual(markdown.read_marker(review['body'], 'advisory')['opinion'], 'ok')
            ((after, _),) = receipts(gh)
            self.assertIn('### Review consultiva', after['body'])
            ((_, record),) = records(gh, 'advisory')
            self.assertEqual((record['outcome'], record['opinion']), ('done', 'ok'))
            self.assertEqual(gh.labels[100], {READY, NEEDS_OPERATOR})
            # Done: the next tick plans nothing for the head.
            snap = d.snapshot()
            self.assertEqual(snap.advisories, [])


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FailureBase(unittest.TestCase):
    """A dispatcher with the ledger and the advisory state in ``state``, on a clock."""

    def make(self, state, *outputs, ws=None):
        self.gh = OperatorGH()
        self.gh.labels[100] = {NEEDS_OPERATOR}
        self.ws = ws or AdvisoryWS()
        self.runner = FakeRunner(*outputs)
        self.logs = []
        self.clock = Clock()
        self.d = Dispatcher(ON, self.gh, self.ws, self.runner, log=self.logs.append,
                            identities=FakeIdentities(self.gh),
                            ledger=Ledger(self.gh, state_dir=state), clock=self.clock,
                            advisory_results=AdvisoryResults(state),
                            advisory_failures=AdvisoryFailures(state))
        return self.d

    def tick(self):
        """Snapshot, plan and run the advisory actions as a lane runs them: an unexpected
        error is reported, not raised. Returns the snapshot and the outcomes."""
        snap = self.d.snapshot()
        prs = {p.number: p for p in snap.prs}
        lane = [(n, a) for n, a in enumerate(plan(snap, ON)) if a.kind == 'advisory']
        outcomes = {}
        self.d._run_lane(lane, prs, outcomes)
        return snap, list(outcomes.values())


class FailureTests(FailureBase):
    """A failed advisory action (#381): one retry per head, after a pause, the turn always
    closed, and no record written twice."""

    def test_a_failed_action_is_tried_once_more_after_a_pause_and_then_no_more(self):
        with tempfile.TemporaryDirectory() as state:
            self.make(state, KeyError('advisory'), KeyError('advisory'), answer())
            snap, outcomes = self.tick()
            self.assertEqual(snap.advisories, [{'number': 100, 'head': SHA1}])
            self.assertEqual(outcomes, ["unexpected error: KeyError: 'advisory'"])
            # The turn is closed as a failure, never left open; nothing else is published.
            ((_, turn),) = ledger_turns(self.gh)
            self.assertEqual((turn['state'], turn['outcome']), ('closed', 'failure'))
            self.assertIn('KeyError', turn['error'])
            self.assertEqual(self.d.ledger.pending(), [])
            self.assertEqual((self.gh.native, records(self.gh, 'advisory')), ([], []))
            self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
            comments = len(self.gh.thread[100])
            # Within the pause the head is not planned, not even after a restart.
            self.clock.now += RETRY_SECONDS - 1
            self.d.advisory_failures = AdvisoryFailures(state)
            snap, outcomes = self.tick()
            self.assertEqual((snap.advisories, outcomes), ([], []))
            self.assertEqual(len(self.runner.calls), 1)
            self.assertEqual(len(self.gh.thread[100]), comments)
            # After it, the one retry: a new turn, closed as a failure too.
            self.clock.now += 1
            snap, outcomes = self.tick()
            self.assertEqual(snap.advisories, [{'number': 100, 'head': SHA1}])
            self.assertEqual(len(self.runner.calls), MAX_ATTEMPTS)
            self.assertEqual([(r['state'], r['outcome']) for _, r in ledger_turns(self.gh)],
                             [('closed', 'failure')] * MAX_ATTEMPTS)
            self.assertIn('no new turn on this head', ' '.join(self.logs))
            comments = len(self.gh.thread[100])
            # Then the head gets no turn again, however long it waits, and nothing is written.
            for _ in range(3):
                self.clock.now += 3600
                snap, outcomes = self.tick()
                self.assertEqual((snap.advisories, outcomes), ([], []))
            self.d._do_advisory(snap.prs[0])  # an action planned earlier does nothing either
            self.assertEqual(len(self.runner.calls), MAX_ATTEMPTS)
            self.assertEqual(len(self.gh.thread[100]), comments)
            self.assertEqual(len(ledger_turns(self.gh)), MAX_ATTEMPTS)
            # A new head starts over, and its review forgets the failures.
            self.gh.head = SHA2
            snap, outcomes = self.tick()
            self.assertEqual(outcomes, ['done'])
            self.assertEqual(ledger_turns(self.gh)[-1][1]['outcome'], 'success')
            self.assertEqual(records(self.gh, 'advisory')[-1][1]['head'], SHA2)
            self.assertIsNone(self.d.advisory_failures.get(100, SHA2))

    def test_a_kept_result_is_published_after_the_failures_without_a_new_turn(self):
        with tempfile.TemporaryDirectory() as state:
            self.make(state, answer())
            comment = no_summary(self.gh)
            for _ in range(MAX_ATTEMPTS + 1):
                _, outcomes = self.tick()
                self.assertEqual(outcomes, ['failed: HTTP 502'])
            self.assertEqual(self.d.advisory_failures.get(100, SHA1)['failures'],
                             MAX_ATTEMPTS + 1)
            # The turn ran once; its result is still published, and the failures forgotten.
            self.gh.comment = comment
            _, outcomes = self.tick()
            self.assertEqual(outcomes, ['done'])
            self.assertEqual(len(self.runner.calls), 1)
            self.assertEqual(len(advisory_reviews(self.gh)), 1)
            self.assertEqual(len(records(self.gh, 'advisory')), 1)
            self.assertIsNone(self.d.advisory_failures.get(100, SHA1))

    def test_a_failure_that_cannot_be_recorded_does_not_hide_the_error(self):
        with tempfile.TemporaryDirectory() as state:
            self.make(state, KeyError('advisory'), KeyError('advisory'), answer())

            def broken(data):
                raise OSError('disk full')
            self.d.advisory_failures._store = broken
            _, outcomes = self.tick()
            self.assertEqual(outcomes, ["unexpected error: KeyError: 'advisory'"])
            self.assertIn('#100   advisory failure of #100 not recorded: disk full: no new turn '
                          'on this head until a restart', self.logs)
            comments = len(self.gh.thread[100])
            # The failure not written still holds the head: no new turn, at the same instant
            # nor later, and nothing written again.
            for step in (0, 0, 0, RETRY_SECONDS, 3600):
                self.clock.now += step
                snap, outcomes = self.tick()
                self.assertEqual((snap.advisories, outcomes), ([], []))
            self.d._do_advisory(snap.prs[0])  # an action planned earlier does nothing either
            self.assertEqual(len(self.runner.calls), 1)
            self.assertEqual(len(ledger_turns(self.gh)), 1)
            self.assertEqual(len(self.gh.thread[100]), comments)
            # A new head starts over.
            self.gh.head = SHA2
            self.assertEqual(self.tick()[1], ["unexpected error: KeyError: 'advisory'"])
            self.assertEqual(len(self.runner.calls), 2)

    def test_the_failures_of_a_head_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as state:
            failures = AdvisoryFailures(state)
            self.assertEqual(failures.record(100, SHA1, 10.0),
                             {'head': SHA1, 'failures': 1, 'retry_at': 10.0 + RETRY_SECONDS})
            self.assertEqual(AdvisoryFailures(state).record(100, SHA1, 20.0)['failures'], 2)
            self.assertEqual(AdvisoryFailures(state).get(100, SHA1)['failures'], 2)
            self.assertIsNone(AdvisoryFailures(state).get(100, SHA2))
            # Another head replaces the entry and starts over.
            self.assertEqual(AdvisoryFailures(state).record(100, SHA2, 30.0)['failures'], 1)
            AdvisoryFailures(state).drop(100)
            self.assertIsNone(AdvisoryFailures(state).get(100, SHA2))


class LogsWS(AdvisoryWS):
    """AdvisoryWS with the dispatcher's logs of a real ``Workspace`` in ``state``."""

    save_evidence = Workspace.save_evidence

    def __init__(self, state):
        super().__init__()
        self.root = Path(state) / 'worktrees'


class TracebackTests(FailureBase):
    """The traceback of an unexpected error of an action, in the local logs (#381)."""

    def test_an_unexpected_error_writes_its_traceback_in_the_logs(self):
        with tempfile.TemporaryDirectory() as state:
            self.make(state, KeyError('advisory'), ws=LogsWS(state))
            self.tick()
            (line,) = [line for line in self.logs if 'unexpected error' in line]
            (path,) = (Path(state) / 'logs').glob('*-advisory-100-traceback.txt')
            self.assertEqual(line, f"#100   unexpected error: KeyError: 'advisory' "
                                   f'(traceback in {path})')
            text = path.read_text(encoding='utf-8')
            self.assertTrue(text.startswith('Traceback (most recent call last):'), text)
            self.assertIn('_advisory_turn', text)
            self.assertIn("KeyError: 'advisory'", text)
            # Never on GitHub.
            self.assertFalse([c for c in self.gh.thread.get(100, [])
                              if 'Traceback' in c['body']])

    def test_the_sequential_tick_writes_it_too(self):
        with tempfile.TemporaryDirectory() as state:
            self.make(state, KeyError('advisory'), ws=LogsWS(state))
            with self.assertRaises(KeyError):
                self.d.tick()
            (path,) = (Path(state) / 'logs').glob('*-advisory-100-traceback.txt')
            self.assertIn(f'(traceback in {path})', ' '.join(self.logs))

    def test_a_traceback_that_cannot_be_written_does_not_hide_the_error(self):
        with tempfile.TemporaryDirectory() as state:
            self.make(state, KeyError('advisory'))  # AdvisoryWS keeps no logs
            _, outcomes = self.tick()
            self.assertEqual(outcomes, ["unexpected error: KeyError: 'advisory'"])
            self.assertIn("#100   unexpected error: KeyError: 'advisory' (traceback not "
                          'written: AttributeError)', self.logs)


if __name__ == '__main__':
    unittest.main()
