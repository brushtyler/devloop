"""devloop: the advisory review of a PR in agent:needs-operator (#369).

The agents are simulated (``FakeRunner``): the tests check when the advisory review runs, how it
is published and that it changes nothing devloop decides on."""

import dataclasses
import sys
import tomllib
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from devloop import config, markdown, receipt  # noqa: E402
from devloop.agents import AgentError  # noqa: E402
from devloop.dispatch_advisory import ADVISORY_TITLE, normalize_advisory  # noqa: E402
from devloop.dispatcher import Dispatcher  # noqa: E402
from devloop.github import GitHubError  # noqa: E402
from devloop.ledger import Ledger  # noqa: E402
from devloop.planning import counted_rounds, plan  # noqa: E402
from helpers.devloop_fakes import SHA1, FakeIdentities  # noqa: E402
from test_devloop import CFG, IMPL, META, REV, FakeRunner, FakeWS, raw_review  # noqa: E402
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
            'opinion': opinion, 'opinion_reason': 'Manca un test del default.'}


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
        data = tomllib.loads(config.DEFAULT_CONFIG.read_text())
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

    def test_a_native_review_without_its_summary_still_counts_for_its_head(self):
        self.make(answer())
        self.tick()
        # The summary lost after the review: the review alone holds the head.
        self.gh.thread[100] = []
        self.assertTrue(self.d._advisory_done(100, SHA1, IMPL))
        # Only the configured agent's identity counts.
        self.gh.native = [(n, dict(r, login=LOGINS[REV])) for n, r in self.gh.native]
        self.assertFalse(self.d._advisory_done(100, SHA1, IMPL))

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
        self.tick()
        self.assertEqual(self.gh.thread.get(100, []), [])
        self.assertEqual(self.gh.labels[100], {NEEDS_OPERATOR})
        self.gh.submit_review = submit
        self.tick()
        self.assertEqual(len(self.gh.native), 1)
        self.assertEqual(len(records(self.gh, 'advisory')), 1)

    def test_labels_or_head_changed_publish_nothing(self):
        self.make(answer())
        snap = self.d.snapshot()
        self.gh.labels[100] = {'agent:review'}
        self.d._do_advisory(snap.prs[0])
        self.assertEqual(self.runner.calls, [])
        # The head moves during the turn: the review of the old head is not published.
        self.gh.labels[100] = {NEEDS_OPERATOR}
        run = self.runner.run

        def moving(*args):
            self.gh.head = SHA2
            return run(*args)
        self.runner.run = moving
        self.d._do_advisory(snap.prs[0])
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(self.gh.native, [])
        self.assertEqual(records(self.gh, 'advisory'), [])

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
        self.assertNotIn('Review consultiva', receipt.section(content))
        self.assertEqual(receipt.digest(content),
                         receipt.digest(receipt.build(**args, advisory=None)))


if __name__ == '__main__':
    unittest.main()
