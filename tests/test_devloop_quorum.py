"""devloop: more reviewers, quorum and aggregation at level 2 (#111, part 1 of #66)."""

import dataclasses
import tomllib
import unittest

from devloop import agents, config, markdown, policy, review
from devloop.dispatcher import Action, Dispatcher, plan
from test_devloop import (CFG, IMPL, SHA1, SHA2, FakeIdentities, FakeRunner, FakeWS, SnapshotGH,
                          finding, fixed, raw_review)

DATA = tomllib.loads(config.DEFAULT_CONFIG.read_text())
# The pool of the default configuration: two reviewers with their own apps and models.
FIRST, SECOND = CFG.reviewers[:2]
CORE = [{'path': 'devloop/dispatcher.py', 'links': []}]  # a sensitive diff: level 2


class Acting:
    """The client of one actor: the comments it writes carry its app's login."""

    def __init__(self, fake, actor):
        self._fake, self._actor = fake, actor

    def __getattr__(self, name):
        method = getattr(self._fake, name)

        def call(*args, **kw):
            self._fake.actor = self._actor
            try:
                return method(*args, **kw)
            finally:
                self._fake.actor = 'dispatcher'
        return call


class Identities(FakeIdentities):
    def github(self, actor, permissions=None):
        return Acting(self.fake, actor)


class QuorumGH(SnapshotGH):
    """PR #100 opened by the implementer's app; every comment carries its writer's login."""

    actor = 'dispatcher'

    def __init__(self, earlier=()):
        super().__init__(earlier)
        self.meta = {}
        self.written = []

    def comment(self, number, body):
        super().comment(number, body)
        self.written.append(FakeIdentities.LOGINS[self.actor])

    def open_prs(self):
        raw = super().open_prs()[0]
        return [dict(raw, body=markdown.publishable('b', 'pr', self.meta), headRefOid=self.head,
                     author={'login': FakeIdentities.LOGINS[IMPL]})]

    def pr_comments(self, number):
        return self.earlier + [{'author': {'login': who}, 'body': body}
                               for (n, body), who in zip(self.comments, self.written)
                               if n == number]


class Quorum(unittest.TestCase):
    def make(self, *outputs, cfg=CFG, diff=CORE, labels=('agent:review',), profile=True):
        self.gh = QuorumGH()
        self.gh.labels[100] = set(labels)
        self.ws = FakeWS()
        self.ws.diff = list(diff)
        self.runner = FakeRunner(*outputs)
        self.logs = []
        self.d = Dispatcher(cfg, self.gh, self.ws, self.runner, log=self.logs.append,
                            identities=Identities(self.gh))
        self.gh.meta = {'issue': 7, 'implementer': IMPL, 'reviewer': cfg.reviewer}
        if profile:
            self.gh.meta['profile'] = self.d._profile(IMPL, 'write')
        return self.d

    def planned(self):
        return plan(self.d.snapshot(), self.d.cfg)

    def records(self, kind):
        return [r for r in (markdown.read_marker(b, kind) for _, b in self.gh.comments) if r]

    def reviewed(self):
        return [call[0] for call in self.runner.calls if call[1] == 'review']


class QuorumTests(Quorum):
    def test_both_independent_approvals_promote_the_pr(self):
        self.make(raw_review(), raw_review())
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        # One approval is not the quorum: no label changes, no promotion.
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.records('policy'), [])
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        reviews = self.records('review')
        self.assertEqual([(r['reviewer'], r['round']) for r in reviews], [(FIRST, 1), (SECOND, 1)])
        for r in reviews:
            self.assertEqual((r['receipt']['level'], r['receipt']['reviewers']),
                             (2, [FIRST, SECOND]))
            self.assertEqual(r['profile'], self.d._profile(r['reviewer'], 'review'))
        (promoted,) = self.records('policy')
        self.assertEqual(promoted['event'], 'promoted')
        # Each reviewer's comment and native review come from its own app.
        self.assertEqual(self.gh.written[:2], [FakeIdentities.LOGINS[FIRST],
                                               FakeIdentities.LOGINS[SECOND]])
        self.assertEqual(len(self.gh.native), 2)
        # Ready: nothing more to do; the replay needs no inference.
        self.assertEqual(self.planned(), [])

    def test_two_approvals_of_the_same_reviewer_are_not_enough(self):
        self.make(raw_review(), raw_review())
        self.d.tick()
        (first,) = self.gh.comments
        # The same reviewer's approval of the same round again, e.g. after a race.
        self.gh.comments.append(first)
        self.gh.written.append(self.gh.written[0])
        snap = self.d.snapshot()
        self.assertEqual(len(snap.prs[0].reviews), 2)
        self.assertEqual(len(review.current_round(snap.prs[0].reviews)), 1)  # deduplicated
        self.assertEqual(plan(snap, CFG), [Action('review', 100, f'the round waits for {SECOND}')])
        # Even handed to promotion directly, two approvals of one reviewer miss the quorum.
        self.d._promote(snap.prs[0], snap.prs[0].reviews)
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        (stale,) = self.records('policy')
        self.assertEqual(stale['event'], 'stale')
        self.assertIn(f'manca l\'approvazione di `{SECOND}`', self.gh.comments[-1][1])

    def test_a_request_for_changes_gives_one_fix_with_every_finding(self):
        self.make(raw_review('request_changes', [finding('a')]),
                  raw_review('request_changes', [finding('b')]), fixed())
        self.d.tick()
        # The first verdict waits for the second: no fix yet.
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(self.planned(), [Action('fix', 100)])
        self.d.tick()
        (fix_call,) = [c for c in self.runner.calls if c[1] == 'write']
        self.assertIn(f'1/{FIRST}/R1-1', fix_call[3])
        self.assertIn(f'1/{SECOND}/R1-1', fix_call[3])
        (fix,) = self.records('fix')
        self.assertEqual((fix['round'], fix['profile']), (1, self.d._profile(IMPL, 'write')))
        self.assertEqual(self.gh.labels[100], {'agent:review'})

    def test_an_approval_and_a_request_for_changes_go_to_the_fixer(self):
        self.make(raw_review(), raw_review('request_changes', [finding()]))
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual(self.records('policy'), [])

    def test_a_needs_human_verdict_stops_the_round_and_a_rereview_reaches_both(self):
        self.make(raw_review('needs_human', human='manca una decisione'), raw_review(),
                  raw_review(), raw_review())
        self.d.tick(), self.d.tick()
        # Not an approval, and the quorum is not lowered: a human decides.
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        self.gh.labels[100] = {'agent:review'}
        self.assertEqual(self.planned(), [Action('apply_review', 100, 'verdict already recorded')])
        self.gh.comments.append((100, '/devloop rereview\n\nDecisione presa: va bene così.'))
        self.gh.written.append('brushtyler')
        self.d.tick(), self.d.tick()
        prompts = [c[3] for c in self.runner.calls[2:]]
        self.assertEqual(self.reviewed()[2:], [FIRST, SECOND])
        for prompt in prompts:
            self.assertIn('Decisione presa: va bene così.', prompt)
            self.assertIn('round 2 of at most 3', prompt)
        # Each reviewer of round 2 judges on its own: the other's verdict is not in its prompt.
        self.assertNotIn('### Round 2', prompts[1])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_the_next_round_closes_findings_by_their_owners_only(self):
        self.make(raw_review(), raw_review('request_changes', [finding()]), fixed(),
                  raw_review('approve', previous=[
                      {'id': f'1/{SECOND}/R1-1', 'status': 'resolved', 'note': 'ok'}]),
                  raw_review('approve', previous=[
                      {'id': f'1/{SECOND}/R1-1', 'status': 'resolved', 'note': 'ok'}]))
        self.d.tick(), self.d.tick(), self.d.tick()  # round 1, then the fix
        self.gh.head = self.ws._head  # the fix pushed a new head
        self.d.tick()
        # The first reviewer's assessment does not close the other's finding, nor downgrade
        # its own approval: the round waits for the owner.
        r2 = self.records('review')[-1]
        self.assertEqual((r2['reviewer'], r2['round'], r2['verdict']), (FIRST, 2, 'approve'))
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(self.reviewed(), [FIRST, SECOND, FIRST, SECOND])


class PoolTests(Quorum):
    def alias_cfg(self, **change):
        """A pool of the first reviewer and an alias of it under another key."""
        alias = dataclasses.replace(CFG.agents[FIRST], key='alias', display_name='Alias', **change)
        return dataclasses.replace(CFG, agents=dict(CFG.agents, alias=alias),
                                   reviewers=(FIRST, 'alias'))

    def assert_needs_human(self, why):
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        (record,) = self.records('policy')
        self.assertEqual((record['event'], record['receipt']['reviewers']), ('level', []))
        self.assertIn(why, self.gh.comments[-1][1])

    def test_two_aliases_with_the_same_tuple_are_one_reviewer(self):
        # Different keys and descriptions, and different identities (the alias has no app):
        # the same (kind, model, effort, review_args) still makes them one.
        self.make(cfg=self.alias_cfg())
        self.assert_needs_human('non ne ha abbastanza')

    def test_a_different_tuple_is_not_enough_with_the_same_identity(self):
        # e.g. an alias that publishes with the same app.
        self.make(cfg=self.alias_cfg(review_args=('--x',)))
        self.d.identities.LOGINS = dict(FakeIdentities.LOGINS, alias=FakeIdentities.LOGINS[FIRST])
        self.assert_needs_human('non ne ha abbastanza')
        # The same alias with an identity of its own is a second reviewer.
        self.d.identities.LOGINS = dict(FakeIdentities.LOGINS, alias='alias[bot]')
        self.assertEqual(self.d._select(self.d.snapshot().prs[0], 2)[0], [FIRST, 'alias'])

    def test_a_reviewer_with_the_implementers_model_is_excluded(self):
        impl = CFG.agents[IMPL]
        twin = dataclasses.replace(CFG.agents[FIRST], kind=impl.kind, model=impl.model,
                                   effort=impl.effort)
        self.make(cfg=dataclasses.replace(CFG, agents=dict(CFG.agents, **{FIRST: twin})))
        self.assert_needs_human('non ne ha abbastanza')

    def test_the_pool_is_scanned_in_order_for_the_first_independent_pair(self):
        cfg = self.alias_cfg()
        cfg = dataclasses.replace(cfg, reviewers=(FIRST, 'alias', SECOND))
        d = self.make(cfg=cfg)
        p = d.snapshot().prs[0]
        self.assertEqual(d._select(p, 2)[0], [FIRST, SECOND])
        self.assertEqual(d._select(p, 1)[0], [CFG.reviewer])

    def test_a_fixer_is_excluded_too(self):
        d = self.make()
        p = d.snapshot().prs[0]
        p.fixes = [{'round': 1, 'head_sha': SHA2, 'responses': [],
                    'profile': d._profile(FIRST, 'review')}]
        self.assertEqual(d._select(p, 2)[0], [])

    def test_who_resolved_conflicts_is_excluded_after_a_config_change(self):
        first = CFG.agents[FIRST]
        twin = dataclasses.replace(CFG.agents[IMPL], kind=first.kind, model=first.model,
                                   effort=first.effort)
        d = self.make({'status': 'completed', 'summary': 'Kept both sides.',
                       'blocked_reason': ''})
        # The implementer's key runs with the first reviewer's model while it resolves the
        # conflicts, then the configuration goes back to what it was.
        d.cfg = dataclasses.replace(CFG, agents=dict(CFG.agents, **{IMPL: twin}))
        p = d.snapshot().prs[0]
        p.merge_state = 'conflict'
        d._do_sync(p)
        self.assertEqual(self.reviewed(), [])
        self.assertEqual(self.gh.written[-1], FakeIdentities.LOGINS[IMPL])
        d.cfg = CFG
        p = d.snapshot().prs[0]
        (sync,) = p.syncs
        self.assertEqual(sync['profile'], dict(d._profile(IMPL, 'write'), model=first.model,
                                               kind=first.kind, effort=first.effort))
        selected, why = d._select(p, 2)
        self.assertEqual(selected, [])
        self.assertIn('non ne ha abbastanza', why)
        # A sync marker without a profile leaves independence unverifiable.
        p.syncs = [{'head_sha': SHA2}]
        self.assertIn('non è verificabile', d._select(p, 2)[1])

    def test_unverifiable_history_goes_to_a_human(self):
        # A PR opened before #111 records no profile of its implementer.
        self.make(profile=False)
        self.assert_needs_human('non è verificabile')
        # Nor does a fix recorded before #111.
        d = self.make()
        p = d.snapshot().prs[0]
        p.fixes = [{'round': 1, 'head_sha': SHA1, 'responses': []}]
        self.assertIn('non è verificabile', d._select(p, 2)[1])

    def test_level_one_ignores_profiles_as_before(self):
        self.make(raw_review(), diff=[{'path': 'x.py', 'links': []}], profile=False)
        self.d.tick()
        self.assertEqual(self.reviewed(), [CFG.reviewer])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})


class RecoveryTests(Quorum):
    def test_after_a_failed_second_review_the_round_resumes_from_it(self):
        self.make(raw_review(), agents.AgentError('codex timed out'), raw_review())
        self.d.tick()
        self.d.tick()
        # A reviewer in timeout is no approval: a human looks first.
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        self.assertEqual(self.records('policy'), [])
        self.gh.labels[100] = {'agent:review'}
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND, SECOND])  # the first is not rerun
        self.assertEqual([r['round'] for r in self.records('review')], [1, 1])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_verdict_on_a_head_that_moved_meanwhile_is_discarded(self):
        test = self

        class Moving(FakeRunner):
            def run(self, agent, *args):
                if agent.key == SECOND:
                    test.gh.head = SHA2  # pushed while the second reviewer works
                return super().run(agent, *args)

        self.make()
        self.runner = self.d.runner = Moving(raw_review(), raw_review())
        self.d.tick()
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual([r['reviewer'] for r in self.records('review')], [FIRST])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertTrue(any('verdict discarded' in line for line in self.logs))
        # The new head starts a new round with every reviewer.
        self.ws._head = SHA2
        self.assertEqual(self.planned(), [Action('review', 100)])
        self.runner.outputs = [raw_review()]
        self.d.tick()
        self.assertEqual([(r['reviewer'], r['round'], r['head_sha'])
                          for r in self.records('review')], [(FIRST, 1, SHA1), (FIRST, 2, SHA2)])

    def test_a_published_round_is_applied_without_new_inference(self):
        # Both reviews were published, then the tick stopped before applying them.
        self.make(raw_review(), raw_review())
        self.d.tick()
        self.d._apply_round = lambda *a, **kw: None  # stops before applying the round
        self.d.tick()
        del self.d._apply_round
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.planned(), [Action('apply_review', 100,
                                                 'approval already recorded')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_reviewer_outside_the_pool_is_bound_by_its_profile(self):
        # Level 1 keeps the PR's recorded reviewer even when the pool no longer lists it, so
        # the policy version does not cover it: the receipt binds its profile instead.
        pool = dataclasses.replace(CFG, reviewer=SECOND, reviewers=(SECOND,))
        for change in ({}, {'model': 'other'}, {'effort': 'low'}, {'review_args': ('--x',)}):
            with self.subTest(change=change):
                self.make(raw_review(), cfg=pool, diff=[{'path': 'x.py', 'links': []}])
                self.gh.meta['reviewer'] = FIRST
                self.d._apply_round = lambda *a, **kw: None  # stops before applying the round
                self.d.tick()
                del self.d._apply_round
                self.assertEqual(self.reviewed(), [FIRST])
                changed = dataclasses.replace(pool, agents=dict(
                    CFG.agents, **{FIRST: dataclasses.replace(CFG.agents[FIRST], **change)}))
                self.assertEqual(changed.review_policy.version(), pool.review_policy.version())
                self.d.cfg = changed
                self.assertEqual(self.planned(), [Action('apply_review', 100,
                                                         'approval already recorded')])
                self.d.tick()
                self.assertEqual(self.reviewed(), [FIRST])
                if not change:
                    self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
                    continue
                self.assertEqual(self.gh.labels[100], {'agent:review'})
                (stale,) = self.records('policy')
                self.assertEqual(stale['event'], 'stale')
                self.assertIn('identità, modello, effort o argomenti dei reviewer',
                              self.gh.comments[-1][1])

    def test_a_round_counts_once_against_the_limit(self):
        cfg = dataclasses.replace(CFG, max_review_rounds=1)
        self.make(raw_review(), raw_review(), cfg=cfg)
        self.d.tick()
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})


class NormalizationTests(unittest.TestCase):
    def test_another_owners_open_finding_does_not_downgrade_an_approval(self):
        first = review.normalize_review(raw_review('request_changes', [finding()]),
                                        previous_open=[], round_no=1, max_rounds=3,
                                        head_sha=SHA1, ci='pass', reviewer=SECOND)
        prev = review.open_findings([first])
        mine = review.normalize_review(raw_review(), previous_open=prev, round_no=2, max_rounds=3,
                                       head_sha=SHA1, ci='pass', reviewer=FIRST)
        self.assertEqual(mine['verdict'], 'approve')
        self.assertEqual(mine['previous_findings'][0]['status'], 'unresolved')
        owner = review.normalize_review(raw_review(), previous_open=prev, round_no=2,
                                        max_rounds=3, head_sha=SHA1, ci='pass', reviewer=SECOND)
        self.assertEqual(owner['verdict'], 'request_changes')

    def test_round_helpers(self):
        a = {'round': 1, 'source': 'agent', 'reviewer': 'x', 'verdict': 'approve'}
        b = dict(a, reviewer='y', verdict='request_changes')
        c = dict(a, reviewer='y', verdict='needs_human')
        self.assertEqual(review.last_round([]), 0)
        self.assertEqual(review.current_round([a, b, dict(b, verdict='approve')]), [a, b])
        self.assertEqual(review.round_verdict([a]), 'approve')
        self.assertEqual(review.round_verdict([a, b]), 'request_changes')
        self.assertEqual(review.round_verdict([b, c]), 'needs_human')


class ConfigTests(unittest.TestCase):
    def test_a_single_reviewer_is_a_pool_of_one(self):
        roles = {'implementer': 'claude', 'reviewer': 'codex'}
        cfg = config.parse_config(dict(DATA, roles=roles))
        self.assertEqual((cfg.reviewer, cfg.reviewers, cfg.pool()), ('codex', ('codex',),
                                                                     ('codex',)))
        self.assertEqual(CFG.reviewers, ('codex', 'agy'))
        self.assertEqual(CFG.reviewer, 'codex')

    def test_invalid_pools_stop_devloop(self):
        for roles in ({'implementer': 'claude', 'reviewer': 'codex', 'reviewers': ['codex']},
                      {'implementer': 'claude', 'reviewers': []},
                      {'implementer': 'claude', 'reviewers': ['codex', 'codex']},
                      {'implementer': 'claude', 'reviewers': ['codex', 'claude']},
                      {'implementer': 'claude', 'reviewers': ['codex', 'nobody']},
                      {'implementer': 'claude', 'reviewers': 'codex'},
                      {'implementer': 'claude'}):
            with self.subTest(roles=roles), self.assertRaises(config.ConfigError):
                config.parse_config(dict(DATA, roles=roles))

    def test_the_policy_version_covers_the_reviewers(self):
        version = CFG.review_policy.version()
        agents_table = DATA['agents']
        # agy's fallback inherits its effort, which may not go below high (#121).
        for change in ({'model': 'other'}, {'effort': 'xhigh'}, {'review_args': ['--x']}):
            with self.subTest(change=change):
                data = dict(DATA, agents=dict(agents_table, agy=dict(agents_table['agy'],
                                                                     **change)))
                self.assertNotEqual(config.parse_config(data).review_policy.version(), version)
        reordered = dict(DATA, roles=dict(DATA['roles'], reviewers=['agy', 'codex']))
        self.assertNotEqual(config.parse_config(reordered).review_policy.version(), version)
        # The implementer is not part of the review policy.
        data = dict(DATA, agents=dict(agents_table, claude=dict(agents_table['claude'],
                                                                model='other')))
        self.assertEqual(config.parse_config(data).review_policy.version(), version)

    def test_independence_rules(self):
        base = {'identity': 'a[bot]', 'kind': 'codex', 'model': 'm', 'effort': 'high', 'args': []}
        self.assertFalse(policy.distinct(base, dict(base, identity='b[bot]')))
        self.assertFalse(policy.distinct(base, dict(base, model='n')))
        self.assertTrue(policy.distinct(base, dict(base, identity='b[bot]', args=['--x'])))
        self.assertFalse(policy.distinct(dict(base, identity=None),
                                         dict(base, identity=None, model='n')))
        writer = dict(base, identity='w[bot]', args=['--write'])
        self.assertFalse(policy.independent(base, writer))  # same model, other arguments
        self.assertTrue(policy.independent(base, dict(writer, effort='low')))
        self.assertIsNone(policy.profile_of({'profile': dict(base, args='x')}))


if __name__ == '__main__':
    unittest.main()
