"""devloop: the findings-only reviewer of the same agent as the implementer (#416, ADR 0005, D8,
integration of 2026-11-19).

With ``review_policy.findings_reviewer``, off by default (D10), a reviewer key outside the pool
reviews each level 2 round of a PR whose implementer signs with the implementer app and is the
same agent, with another model: claude-reviewer with Sonnet beside codex and agy on the code of
claude-implementer with Opus. Its findings join the round in progress and go to the fix; the
reviewers of the quorum close them. Its approval never counts for the quorum, and it never takes
the place of a missing reviewer.
"""

import dataclasses
import tempfile
import tomllib
import unittest
from unittest import mock

from devloop import config, ledger, markdown, receipt, review
from devloop.config import ConfigError
from devloop.dispatcher import Dispatcher
from devloop.planning import counted_rounds, pending_reviewers
from devloop.review import FINDINGS_ONLY
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH, FakeIdentities
from test_devloop import CFG, FakeRunner, FakeWS, finding, fixed, pr, raw_review
from test_devloop_fallback import unavailable
from test_devloop_implementer_app import (IMPL_APP, SONNET, RoleIdentities, migrated, parse,
                                          role_meta)
from test_devloop_quorum import CORE, Identities, Quorum, QuorumGH

DATA = ssdlc_project.data()
KEY = 'claude-reviewer'
QUORUM = ['codex-reviewer', 'agy-reviewer']
# The implementer's model, and an effort allowed for a fallback other than its.
OPUS = DATA['agents']['claude']['model']
OTHER_EFFORT = 'max' if DATA['agents']['claude']['effort'] == 'xhigh' else 'xhigh'


def findings_data(key=KEY, agents=None, **roles):
    """The keys by role of P5-3, codex and agy in the pool, claude-reviewer with Sonnet outside
    it, and ``findings_reviewer`` set to ``key`` (None: left out, the default)."""
    data = migrated(dict({KEY: SONNET}, **(agents or {})), **roles)
    if key is not None:
        data['review_policy'] = dict(DATA['review_policy'], findings_reviewer=key)
    return data


FINDINGS = parse(findings_data())
OFF = parse(findings_data(None))


def change(cfg=FINDINGS, key='claude-implementer', reviews=()):
    made = pr(reviews=reviews)
    made.meta = role_meta(cfg, key)
    return made


def level(n, reviewers=QUORUM):
    return {'pr': 100, 'base': 'b' * 40, 'head': 'a' * 40, 'policy': 'p', 'level': n,
            'reasons': [], 'reviewers': list(reviewers), 'profiles': []}


class ConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(ssdlc_project.load().review_policy.findings_reviewer, '')
        self.assertEqual(OFF.review_policy.findings_reviewer, '')
        self.assertEqual(FINDINGS.review_policy.findings_reviewer, KEY)
        # Out of the policy version, as the advisory review: turning it on voids no approval.
        self.assertEqual(FINDINGS.review_policy.version(), OFF.review_policy.version())
        self.assertIn('findings_reviewer', config.OVERLAY_KEYS['review_policy'])

    def test_never_in_the_pool(self):
        for data, error in (
                (findings_data(reviewers=[*QUORUM, KEY]),
                 "review_policy.findings_reviewer: 'claude-reviewer' is in the reviewer pool"),
                (findings_data('claude-implementer'),
                 "'claude-implementer' is a implementer key"),
                (findings_data('nobody-reviewer'), "unknown agent 'nobody-reviewer'"),
                (findings_data(1), 'expected a string')):
            with self.subTest(error=error):
                with self.assertRaises(ConfigError) as ctx:
                    config.parse_config(data)
                self.assertIn(error, str(ctx.exception))


class SelectionTests(unittest.TestCase):
    def make(self, cfg=FINDINGS):
        self.logs = []
        gh = FakeGH()
        return dispatcher(cfg, gh, FakeWS(), FakeRunner(), self.logs.append)

    def test_the_same_agent_at_level_two(self):
        d = self.make()
        self.assertEqual(d._findings_reviewers(change(), level(2)), [KEY])
        self.assertEqual(d._findings_reviewers(change(), level(1, QUORUM[:1])), [])
        # Never a reviewer of the quorum, whatever the pool lacks.
        self.assertEqual(d._select(change(), 2), (QUORUM, ''))
        alone = self.make(dataclasses.replace(FINDINGS, reviewers=('codex-reviewer',),
                                              reviewer='codex-reviewer'))
        reviewers, problem = alone._select(change(), 2)
        self.assertEqual(reviewers, [])
        self.assertIn('non ne ha abbastanza', problem)

    def test_only_on_the_implementers_agent_with_another_model(self):
        # codex implements: claude is not the same agent.
        cfg = parse(findings_data(agents={'codex-implementer': dict(DATA['agents']['codex'],
                                                                     app='implementer')}))
        self.assertEqual(self.make(cfg)._findings_reviewers(change(cfg, 'codex-implementer'),
                                                            level(2)), [])
        # The implementer's very model: not independent of the author.
        cfg = parse(findings_data(agents={KEY: dict(SONNET, model='claude-opus-5-5')}))
        self.assertEqual(self.make(cfg)._findings_reviewers(change(cfg), level(2)), [])
        self.assertIn(f'findings-only reviewer {KEY} skipped', '\n'.join(self.logs))

    def test_the_same_model_with_another_effort_is_not_another_model(self):
        # Independent by (kind, model, effort), but the very model of the implementer: the
        # issue asks for another model, so it does not review.
        for effort in ('low', OTHER_EFFORT):
            with self.subTest(effort=effort):
                self.logs = []
                cfg = parse(findings_data(agents={KEY: dict(SONNET, model=OPUS, effort=effort)}))
                d = self.make(cfg)
                self.assertNotEqual(d._profile(KEY, 'review')['effort'],
                                    change(cfg).meta['profile']['effort'])
                self.assertEqual(d._findings_reviewers(change(cfg), level(2)), [])
                self.assertIn(f'findings-only reviewer {KEY} skipped', '\n'.join(self.logs))

    def test_only_with_the_implementer_app(self):
        # The keys of before: claude implements with its agent's app, not the implementer app.
        sonnet = {k: v for k, v in SONNET.items() if k != 'app'}
        cfg = parse(dict(DATA, agents=dict(DATA['agents'], sonnet=sonnet),
                         review_policy=dict(DATA['review_policy'], findings_reviewer='sonnet')))
        d = self.make(cfg)
        made = pr()
        made.meta = dict(made.meta, profile=d._profile(cfg.implementer, 'write'))
        self.assertEqual(d._findings_reviewers(made, level(2, ['codex', 'agy'])), [])

    def test_off(self):
        self.assertEqual(self.make(OFF)._findings_reviewers(change(OFF), level(2)), [])


EXAMPLE_KEY = 'claude-findings'
REFERENCE = ssdlc_project.ROOT / 'docs' / 'devloop' / 'reference.md'


def uncommented_config():
    """The text of ``devloop/config.toml`` with its commented example of the findings-only
    reviewer uncommented (#512): the line of ``findings_reviewer`` and the table of its key,
    up to the first line that is not a comment of it."""
    lines = config.DEFAULT_CONFIG.read_text(encoding='utf-8').split('\n')
    found = {'line': False, 'table': False}
    for i, line in enumerate(lines):
        if line.startswith('# findings_reviewer = '):
            lines[i] = line[2:]
            found['line'] = True
        elif line == f'# [agents.{EXAMPLE_KEY}]':
            found['table'] = True
            for j in range(i, len(lines)):
                if not lines[j].startswith('# '):
                    break
                lines[j] = lines[j][2:]
    if not all(found.values()):
        raise AssertionError(f'config.toml: the commented example is missing: {found}')
    return '\n'.join(lines)


def reference_example():
    """The TOML block of the section "Findings-only reviewer" of ``reference.md``."""
    text = REFERENCE.read_text(encoding='utf-8')
    section = text[text.index('### Findings-only reviewer'):]
    block = section[section.index('```toml\n') + len('```toml\n'):]
    return tomllib.loads(block[:block.index('```')])


class ExampleTests(unittest.TestCase):
    """The examples of ``findings_reviewer`` in config.toml and reference.md (#512): since #499
    claude-reviewer is in the pool, and an example naming it would stop devloop at startup."""

    def check(self, cfg):
        self.assertEqual(cfg.review_policy.findings_reviewer, EXAMPLE_KEY)
        self.assertNotIn(EXAMPLE_KEY, cfg.reviewers)
        self.assertIn(KEY, cfg.reviewers)
        self.assertEqual(cfg.implementer, 'claude-implementer')
        self.assertNotEqual(cfg.agent(EXAMPLE_KEY).model, cfg.agent(cfg.implementer).model)
        # It reviews a level 2 round of claude-implementer besides the quorum.
        d = dispatcher(cfg, FakeGH(), FakeWS(), FakeRunner(), lambda _: None)
        self.assertEqual(d._findings_reviewers(change(cfg), level(2)), [EXAMPLE_KEY])

    def test_the_config_example_uncommented_is_accepted(self):
        raw = config._merge(tomllib.loads(uncommented_config()),
                            {**ssdlc_project.identity(), **ssdlc_project.project()})
        # Its own app, in the overlay as the comment says: [apps.claude] is claude-reviewer's.
        app = {'apps': {EXAMPLE_KEY: {'app_id': 1234567, 'slug': f'x-{EXAMPLE_KEY}'}}}
        with self.assertRaises(ConfigError) as ctx:
            config.parse_config(raw, config.INSTALL_ROOT)
        self.assertIn(f'no [apps.{EXAMPLE_KEY}] table', str(ctx.exception))
        self.check(config.parse_config(config.apply_overlay(raw, app), config.INSTALL_ROOT))

    def test_the_reference_example_is_accepted_in_the_overlay(self):
        example = reference_example()
        self.assertEqual(example['review_policy']['findings_reviewer'], EXAMPLE_KEY)
        raw = config.apply_overlay(ssdlc_project.by_role(), example)
        self.check(config.parse_config(raw, config.INSTALL_ROOT))

    def test_no_example_names_a_key_of_the_pool(self):
        pool = ssdlc_project.load_by_role().reviewers
        for where, text in (('config.toml', config.DEFAULT_CONFIG.read_text(encoding='utf-8')),
                            ('reference.md', REFERENCE.read_text(encoding='utf-8'))):
            for key in pool:
                with self.subTest(where=where, key=key):
                    self.assertNotIn(f'findings_reviewer = "{key}"', text)


def dispatcher(cfg, gh, ws, runner, log):
    return Dispatcher(cfg, gh, ws, runner, log=log, identities=RoleIdentities(gh))


def record(reviewer, round_no, verdict='approve', blocking=(), previous=(), role=None,
           reviewers=QUORUM):
    """A keyed review record as ``normalize_review`` writes it, with the round's receipt."""
    made = {'source': 'agent', 'reviewer': reviewer, 'verdict': verdict, 'summary': 's',
            'human_reason': '', 'round': round_no, 'max_rounds': 3, 'head_sha': 'a' * 40,
            'ci': 'pass', 'note': '', 'checks_performed': [], 'non_blocking_findings': [],
            'blocking_findings': [dict(finding(), id=f'R{round_no}-{i}',
                                       key=f'{round_no}/{reviewer}/R{round_no}-{i}')
                                  for i, _ in enumerate(blocking, 1)],
            'previous_findings': [{'id': k.split('/')[2], 'key': k, 'status': s, 'note': ''}
                                  for k, s in previous],
            'receipt': dict(level(2, reviewers))}
    if role:
        made['role'] = role
    return made


F = f'1/{KEY}/R1-1'


class FindingTests(unittest.TestCase):
    """The findings-only findings: they go to the fix, and the quorum closes them."""

    def round1(self):
        return [record('codex-reviewer', 1), record('agy-reviewer', 1),
                record(KEY, 1, 'request_changes', [finding()], role=FINDINGS_ONLY)]

    def test_its_request_for_changes_sends_the_round_to_the_fix(self):
        reviews = self.round1()
        self.assertEqual(review.round_verdict(reviews), 'request_changes')
        (left,) = review.open_findings(reviews)
        self.assertEqual((left['key'], left['owner'], left['findings_only']), (F, KEY, True))

    def test_its_verdict_counts_only_through_its_findings(self):
        quorum = [record('codex-reviewer', 1), record('agy-reviewer', 1)]
        for verdict in ('approve', 'needs_operator', 'unproven'):
            with self.subTest(verdict=verdict):
                only = record(KEY, 1, verdict, role=FINDINGS_ONLY)
                only['operator_reason'] = review.TRUST_POLICY
                self.assertEqual(review.flow_verdict(only), 'approve')
                self.assertFalse(review.trust_policy(only))
                self.assertEqual(review.round_verdict(quorum + [only]), 'approve')
                self.assertEqual(review.trust_policy_parts(quorum + [only]), [])
                self.assertEqual(markdown.native_event(only), 'COMMENT')
        # Without the quorum's approvals the round is not approved, whatever it says.
        rejected = [record('codex-reviewer', 1),
                    record('agy-reviewer', 1, 'request_changes', [finding()]),
                    record(KEY, 1, role=FINDINGS_ONLY)]
        self.assertEqual(review.round_verdict(rejected), 'request_changes')

    def test_the_quorum_closes_its_findings(self):
        round1 = self.round1()
        # Its own assessment closes nothing, nor does one reviewer of the quorum alone.
        for previous in ({KEY: 'resolved', 'codex-reviewer': 'resolved',
                          'agy-reviewer': 'unresolved'},
                         {KEY: 'resolved', 'codex-reviewer': 'resolved'}):
            with self.subTest(previous=previous):
                round2 = [record(who, 2, previous=[(F, status)],
                                 role=FINDINGS_ONLY if who == KEY else None)
                          for who, status in previous.items()]
                state = review.finding_state(round1 + round2)
                self.assertEqual([f['key'] for f in state.open], [F])
                self.assertEqual(state.quorum_closed, {})
        round2 = [record('codex-reviewer', 2, previous=[(F, 'resolved')]),
                  record('agy-reviewer', 2, previous=[(F, 'withdrawn')])]
        state = review.finding_state(round1 + round2)
        self.assertEqual(state.open, [])
        self.assertEqual(state.quorum_closed, {F: {'status': 'resolved', 'round': 2,
                                                   'by': 'codex-reviewer, agy-reviewer'}})
        # The receipt of the PR says who closed it, and that its reviewer is finding-only.
        content = receipt._findings(round1 + round2, ())
        self.assertEqual(content, [{'key': F, 'status': 'resolved', 'round': 2, 'author': None,
                                    'by': 'codex-reviewer, agy-reviewer'}])
        rounds = receipt._rounds(round1)
        self.assertEqual(rounds[0]['verdicts'][2], {'reviewer': KEY,
                                                    'verdict': 'request_changes',
                                                    'role': FINDINGS_ONLY})
        self.assertNotIn('role', rounds[0]['verdicts'][0])
        self.assertIn(f'`{KEY}` (finding-only)', '\n'.join(receipt._rounds_text(rounds, None)))

    def test_a_reviewer_of_the_quorum_does_not_approve_over_it(self):
        (left,) = review.open_findings(self.round1())
        for reviewer, role, verdict in (('codex-reviewer', None, 'request_changes'),
                                        (KEY, FINDINGS_ONLY, 'approve')):
            with self.subTest(reviewer=reviewer):
                made = review.normalize_review(
                    raw_review(previous=[{'id': F, 'status': 'unresolved', 'note': 'no'}]),
                    previous_open=[left], round_no=2, max_rounds=3, head_sha='a' * 40,
                    ci='pass', reviewer=reviewer, role=role)
                self.assertEqual(made['verdict'], verdict)
                self.assertEqual(made['previous_findings'][0]['findings_only'], True)
        made = review.normalize_review(raw_review(), previous_open=[left], round_no=2,
                                       max_rounds=3, head_sha='a' * 40, ci='pass',
                                       reviewer=KEY, role=FINDINGS_ONLY)
        self.assertEqual(made['role'], FINDINGS_ONLY)
        self.assertIn('Findings-only review', made['note'])  # in English by default (#632)
        self.assertIn('Review di soli finding, round 2/3',
                      markdown.render_review(made, 'h', language='Italian'))
        # In English by default (#632).
        self.assertIn('Findings-only review, round 2/3', markdown.render_review(made, 'h'))

    def test_the_ledger_records_its_turn_as_finding_only(self):
        turn = {'type': 'turn', 'state': 'open', 'kind': 'review', 'agent': KEY,
                'identity': 'claude-agent[bot]', 'model': 'claude-sonnet-5-5',
                'effort': 'high', 'started_at': 't', 'role': 'findings'}
        t = ledger.TEXTS.texts('it')
        self.assertIn('- **Ruolo:** `finding-only`', '\n'.join(ledger._render_turn(t, turn)))
        self.assertNotIn('Ruolo', '\n'.join(ledger._render_turn(t, dict(turn, role=None))))
        english = '\n'.join(ledger._render_turn(ledger.TEXTS.texts('en'), turn))
        self.assertIn('- **Role:** `finding-only`', english)
        # And the receipt of the PR, which keeps the role only of such a turn.
        kept = receipt._turn(dict(turn, state='closed'), 'pr')
        self.assertEqual(kept['role'], FINDINGS_ONLY)
        self.assertNotIn('role', receipt._turn(dict(turn, role=None), 'pr'))
        self.assertIn('`review` (finding-only)', '\n'.join(receipt._turns(receipt.TEXTS.texts('English'),
                                                                        [kept])))

    def test_the_ledger_writes_the_role_of_its_turn(self):
        gh = FakeGH()
        with tempfile.TemporaryDirectory() as tmp:
            book = ledger.Ledger(gh, state_dir=tmp)
            for role in (FINDINGS_ONLY, None):
                book.open_turn(100, kind='review', agent=KEY, identity=None, mode='review',
                               model='m', effort='high', fallback_for=None, label='l',
                               role=role)
        turns = [markdown.read_marker(body, 'ledger') for _, body in gh.comments]
        self.assertEqual([t.get('role') for t in turns], [FINDINGS_ONLY, None])
        self.assertNotIn('role', turns[1])
        self.assertIn('finding-only', gh.comments[0][1])
        self.assertNotIn('finding-only', gh.comments[1][1])


class RoleGH(QuorumGH):
    def open_prs(self):
        return [dict(raw, author={'login': IMPL_APP}) for raw in super().open_prs()]


class RoundTests(Quorum):
    """A level 2 round, end to end, with the keys by role: codex and agy review, and
    claude-reviewer reviews for findings only, each in an action of its own."""

    def setUp(self):
        patcher = mock.patch.dict(FakeIdentities.LOGINS, {'implementer': IMPL_APP})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, *outputs, cfg=FINDINGS):
        self.gh = RoleGH()
        self.gh.labels[100] = {'agent:review'}
        self.ws = FakeWS()
        self.ws.diff = list(CORE)  # a sensitive diff: level 2
        self.runner = FakeRunner(*outputs)
        self.logs = []
        self.d = Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                            identities=Identities(self.gh))
        self.gh.meta = role_meta(cfg)
        return self.d

    def test_its_findings_join_the_round_and_go_to_the_fix(self):
        self.make(raw_review(), raw_review(),
                  raw_review('request_changes', [finding('only')]), fixed())
        with mock.patch.object(self.d, '_open_turn', wraps=self.d._open_turn) as opened:
            for _ in range(3):
                self.d.tick()
        self.assertEqual(self.reviewed(), [*QUORUM, KEY])
        roles = [call.args[4] if len(call.args) > 4 else None for call in opened.call_args_list]
        self.assertEqual(roles, [None, None, FINDINGS_ONLY])
        # Its prompt says its role; the quorum's do not.
        prompts = [call[3] for call in self.runner.calls]
        self.assertNotIn('Your role in this round: findings only', prompts[0])
        self.assertIn('Your role in this round: findings only', prompts[2])
        reviews = self.records('review')
        self.assertEqual([(r['reviewer'], r['round'], r.get('role')) for r in reviews],
                         [('codex-reviewer', 1, None), ('agy-reviewer', 1, None),
                          (KEY, 1, FINDINGS_ONLY)])
        # One round: the findings-only review is not a round of its own.
        self.assertEqual(counted_rounds(reviews), 1)
        for r in reviews:
            self.assertEqual((r['receipt']['reviewers'], r['receipt']['findings']),
                             (QUORUM, [KEY]))
        # Its native review is a COMMENT, under its agent's app.
        self.assertEqual([n['event'] for _, n in self.gh.native],
                         ['APPROVE', 'APPROVE', 'COMMENT'])
        self.assertEqual(self.gh.native[2][1]['login'], FakeIdentities.LOGINS['claude'])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(self.records('policy'), [])
        self.d.tick()
        (fix_call,) = [c for c in self.runner.calls if c[1] == 'write']
        self.assertIn(F, fix_call[3])
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_the_round_waits_for_it(self):
        self.make(raw_review(), raw_review(), raw_review())
        self.d.tick()
        self.d.tick()
        # Both reviewers of the quorum approved: the round still waits for its findings.
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(pending_reviewers(self.d.snapshot().prs[0].reviews), [KEY])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        (promoted,) = self.records('policy')
        self.assertEqual(promoted['receipt']['reviewers'], QUORUM)
        self.assertNotIn('findings', promoted['receipt'])

    def test_its_approval_does_not_count_for_the_quorum(self):
        self.make(raw_review(), raw_review('request_changes', [finding()]), raw_review())
        for _ in range(3):
            self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        snap = self.d.snapshot().prs[0]
        codex, _, only = review.current_round(snap.reviews)
        self.assertEqual((only['reviewer'], only['verdict']), (KEY, 'approve'))
        # Even handed to promotion, codex's approval and its own miss agy's.
        self.d._promote(snap, [codex, only])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        (stale,) = self.records('policy')
        self.assertEqual(stale['event'], 'stale')
        self.assertIn('manca l\'approvazione di `agy-reviewer`', self.gh.comments[-1][1])

    def test_it_never_replaces_a_missing_reviewer(self):
        cfg = dataclasses.replace(FINDINGS, reviewers=('codex-reviewer',),
                                  reviewer='codex-reviewer')
        self.make(raw_review(), cfg=cfg)
        self.d.tick()
        self.assertEqual(self.reviewed(), [])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        (escalated,) = self.records('policy')
        self.assertEqual((escalated['event'], escalated['receipt']['reviewers']), ('level', []))

    def test_its_fallback_is_never_the_implementers_model(self):
        # Its primary model is unavailable, and its fallback is the implementer's very model with
        # another effort: independent by (kind, model, effort), but not another model. Without
        # the ban of P5-6, only the findings-only rule keeps it from running.
        def cfg_with(fallback):
            agent = dict(SONNET, fallback_models=[{'model': fallback, 'model_label': 'F',
                                                   'effort': OTHER_EFFORT}])
            made = parse(findings_data(agents={KEY: agent}))
            return dataclasses.replace(made, review_policy=dataclasses.replace(
                made.review_policy, fallback_provider_ban=False))

        self.make(raw_review(), raw_review(), unavailable(KEY), cfg=cfg_with(OPUS))
        for _ in range(3):
            self.d.tick()
        # The fallback never ran: one call for its primary model only, and the turn failed.
        self.assertEqual([c[0] for c in self.runner.calls], [*QUORUM, KEY])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn(f'- `{OPUS}`: not independent', self.gh.comments[-1][1])
        # Another model as the fallback runs, and its review joins the round.
        self.make(raw_review(), raw_review(), unavailable(KEY), raw_review(),
                  cfg=cfg_with('claude-haiku-5-5'))
        for _ in range(3):
            self.d.tick()
        self.assertEqual([c[0] for c in self.runner.calls], [*QUORUM, KEY, KEY])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_off_nothing_changes(self):
        self.make(raw_review(), raw_review(), cfg=OFF)
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.reviewed(), QUORUM)
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        for r in self.records('review'):
            self.assertNotIn('role', r)
            self.assertNotIn('findings', r['receipt'])
        # The legacy configuration of the installation is off too.
        self.assertEqual(CFG.review_policy.findings_reviewer, '')


if __name__ == '__main__':
    unittest.main()
