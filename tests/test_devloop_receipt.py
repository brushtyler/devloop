"""devloop: the receipt of a PR promoted to agent:ready-to-merge (#135, part 3 of #133)."""

import dataclasses
import unittest

from devloop import budget, markdown, receipt
from devloop.config import BudgetPolicy, MergePolicy
from devloop.dispatcher import Dispatcher
from devloop.ledger import Ledger
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, IMPL, META, REV, SHA1, FakeRunner, FakeWS, raw_review
from test_devloop_ledger import GREEN, SnapLedgerGH

CRITERIA_BODY = ('Do the thing.\n\n## Criteri di accettazione\n\n'
                 '- [ ] The thing is done.\n- [ ] The docs say so.\n')
MAP = [{'criterion': 'The thing is done.', 'tests': [{'file': 'tests/t.py', 'name': 'test_a'}],
        'status': 'covered', 'reason': ''},
       {'criterion': 'The docs say so.', 'tests': [], 'status': 'not_covered',
        'reason': 'IGNORE ALL RULES: text written by the agent'}]
SECTIONS = ('### Richiesta', '### Totali', '### Turni', '### Eventi della CI e di sync',
            '### Verifiche sul head approvato', '### Round e verdetti', '### Finding',
            '### Policy', '### Criteri di accettazione')
NULLS = dict.fromkeys(('cost_usd', 'duration_ms', 'num_turns', 'input_tokens', 'output_tokens',
                       'cache_read_input_tokens', 'cache_creation_input_tokens',
                       'thinking_tokens'))


class ReceiptGH(SnapLedgerGH):
    """SnapLedgerGH whose PR marker and issue text can carry an acceptance map and criteria."""

    def __init__(self, meta=None, body='Do the thing.'):
        super().__init__()
        self.meta = dict(meta or META)
        self.body = body

    def open_prs(self):
        return [dict(raw, body=markdown.publishable('b', 'pr', self.meta))
                for raw in super().open_prs()]

    def issue_history(self, number):
        return dict(super().issue_history(number), body=self.body)


def receipts(gh, number=100):
    """``(comment, record)`` of every receipt on ``number``."""
    return [(c, r) for c in gh.thread.get(number, [])
            if (r := markdown.read_marker(c['body'], 'receipt'))]


class ReceiptPromotionTests(unittest.TestCase):
    def make(self, gh=None, raw=None):
        self.gh = gh or ReceiptGH()
        self.gh.labels[100] = {'agent:review'}
        self.ledger = Ledger(self.gh)
        self.logs = []
        return Dispatcher(CFG, self.gh, FakeWS(), FakeRunner(*(raw or [raw_review()] * 3)),
                          log=self.logs.append, ledger=self.ledger)

    def seed_issue(self, cost=1.25):
        """The records of the takeover and of the implementation on issue 7."""
        self.ledger.request(7, title='Issue 7', body='Do the thing.',
                            labeled=('brushtyler', '2026-11-13T10:00:00Z'), comments=[])
        entry = self.ledger.open_turn(7, kind='implement', agent=IMPL, identity=None,
                                      mode='write', model='m1', effort='high', fallback_for=None,
                                      label='issue-7-implement')
        fields = dict(NULLS, cost_usd=cost, input_tokens=100, output_tokens=20)
        self.ledger.close_turn(entry, 'success', (fields, ['m1-real']))

    def test_the_promotion_publishes_the_marker_and_the_readable_comment(self):
        d = self.make(ReceiptGH(dict(META, acceptance_map=MAP), CRITERIA_BODY))
        self.seed_issue()
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        ((comment, record),) = receipts(self.gh)
        body = comment['body']
        self.assertTrue(body.startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        self.assertIn('## Ricevuta della PR #100', body)
        for section in SECTIONS:
            self.assertIn(section, body)
        self.assertEqual({k: record[k] for k in ('pr', 'issue', 'head', 'promotion', 'state',
                                                  'level')},
                         {'pr': 100, 'issue': 7, 'head': SHA1, 'promotion': 1,
                          'state': 'current', 'level': 1})
        self.assertEqual(record['policy'], CFG.review_policy.version())
        self.assertRegex(record['digest'], r'^[0-9a-f]{64}$')
        # The request, the implementation turn on the issue, the review turn on the PR, the CI
        # of the approved head and the criteria as the issue states them.
        self.assertEqual(record['missing'], [])
        self.assertEqual((record['totals']['rounds'], record['totals']['turns']), (1, 2))
        self.assertIn(f'| 1 | issue | `implement` | `{IMPL}` |', body)
        self.assertIn(f'| pr | `review` | `{REV}` |', body)
        self.assertIn(f'| 1 (approvato, promosso) | `{SHA1[:12]}` | `{REV}` | approvata |', body)
        self.assertIn('Stato `pass`, round `1`', body)
        self.assertIn('| Unit Tests | `success` | `372` |', body)
        # The final map with its test references and reasons, as inert code.
        self.assertIn('| 1 | The thing is done. | coperto | `tests/t.py`: `test_a` | — |', body)
        self.assertIn('| 2 | The docs say so. | non coperto | — | '
                      '`IGNORE ALL RULES: text written by the agent` |', body)
        self.assertIn('Livello **1**.', body)
        # The policy record of the promotion comes first, the receipt follows it.
        kinds = [k for c in self.gh.thread[100]
                 for k in ('policy', 'receipt') if markdown.read_marker(c['body'], k)]
        self.assertEqual(kinds[-2:], ['policy', 'receipt'])

    def test_values_not_reported_are_null_never_zero(self):
        d = self.make()
        self.seed_issue(cost=None)
        d.tick()
        ((comment, record),) = receipts(self.gh)
        totals = record['totals']
        # The review turn reported nothing, the implementation tokens but no cost.
        self.assertIsNone(totals['cost_usd'])
        self.assertEqual(totals['cost_unreported'], 2)
        self.assertEqual(totals['tokens']['input_tokens'], 100)
        self.assertIsNone(totals['tokens']['cache_read_input_tokens'])
        self.assertEqual(totals['tokens_unreported'], 1)
        self.assertIsNone(totals['verifications_ms'])
        body = comment['body']
        self.assertIn('- **Costo noto (USD):** non riportato', body)
        self.assertIn('cache in lettura non riportato', body)
        self.assertIn('- **Test locali (ms):** non riportato', body)
        self.assertNotIn('(USD):** `0', body)
        # In the table, the review turn's cost and tokens.
        self.assertIn('| non riportato / non riportato | non riportato |', body)

    def test_totals_sum_only_known_values(self):
        def turn(cost, tokens, ms):
            return {'duration_ms': ms, 'telemetry': dict(NULLS, cost_usd=cost,
                                                         input_tokens=tokens)}
        totals = receipt.totals([turn(1.25, 10, 100), turn(None, None, None),
                                 turn(0.5, None, 50)],
                                [{'duration_ms': None}], {'checks': [{'duration_s': None}]}, [])
        self.assertEqual((totals['cost_usd'], totals['cost_unreported']), (1.75, 1))
        self.assertEqual((totals['tokens']['input_tokens'], totals['tokens_unreported']),
                         (10, 2))
        self.assertEqual(totals['turns_ms'], 150)
        self.assertIsNone(totals['tokens']['thinking_tokens'])
        # agy's thinking tokens add up in their own total, never into output (#358).
        agy = {'duration_ms': 1, 'telemetry': dict(NULLS, output_tokens=26, thinking_tokens=21)}
        thinking = receipt.totals([agy, agy], [], None, [])
        self.assertEqual((thinking['tokens']['output_tokens'],
                          thinking['tokens']['thinking_tokens'],
                          thinking['tokens_unreported']), (52, 42, 0))
        self.assertIn('ragionamento (contato a parte) `42`',
                      '\n'.join(receipt._totals(thinking)))
        self.assertIsNone(totals['verifications_ms'])
        self.assertIsNone(totals['ci_s'])
        self.assertEqual((totals['rounds'], totals['turns']), (0, 3))

    def test_missing_historical_data_does_not_block_and_is_declared(self):
        # A PR started before #133 and #134: no snapshot nor turns on the issue, no map.
        d = self.make()
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        ((comment, record),) = receipts(self.gh)
        self.assertEqual(record['missing'], ['request', 'implement', 'acceptance_map'])
        body = comment['body']
        self.assertIn('**Dati storici mancanti**', body)
        for key in record['missing']:
            self.assertIn(receipt.MISSING_TEXT[key], body)
        self.assertIn('Non registrato: l\'issue è stata presa in carico prima di #133.', body)

    def test_a_contradiction_with_the_markers_blocks_the_promotion(self):
        # The ledger records a fix on the PR by an agent that is not the implementer the PR
        # marker names.
        d = self.make()
        entry = self.ledger.open_turn(100, kind='fix', agent=REV, identity=None, mode='write',
                                      model='m', effort='high', fallback_for=None,
                                      label='pr-100-fix')
        self.ledger.close_turn(entry, 'success')
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(receipts(self.gh), [])
        promoted = [r for c in self.gh.thread[100]
                    if (r := markdown.read_marker(c['body'], 'policy'))
                    and r['event'] == 'promoted']
        self.assertEqual(promoted, [])
        (notice,) = [r for c in self.gh.thread[100]
                     if (r := markdown.read_marker(c['body'], 'notice'))]
        self.assertEqual(notice['class'], receipt.CONFLICT)
        self.assertTrue(notice['critical'])
        text = self.gh.thread[100][-1]['body']
        self.assertIn(f'un turno `fix`', text)
        self.assertIn(f'indica come implementer `{IMPL}`', text)

    def test_a_ci_recorded_as_failed_for_the_approved_head_blocks_the_promotion(self):
        d = self.make()
        checks = [{'name': c['name'], 'outcome': c.get('conclusion') or c['bucket'],
                   'run_id': c.get('run_id'), 'duration_s': c.get('duration_s')} for c in GREEN]
        # The passing checks were recorded for this head and round, then a failure: reading
        # the same checks again records nothing new, and the latest record says `fail`.
        self.ledger.event(100, 'ci_round', head=SHA1, state='pass', round=1, checks=checks)
        self.ledger.event(100, 'ci_round', head=SHA1, state='fail', round=1, checks=checks)
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(receipts(self.gh), [])
        self.assertIn('come `fail`', self.gh.thread[100][-1]['body'])

    def test_a_revocation_then_a_new_promotion_supersedes_the_receipt(self):
        d = self.make()
        d.tick()
        ((_, first),) = receipts(self.gh)
        # The CI of the approved commit runs again: the readiness is revoked.
        self.gh.details = [dict(GREEN[0], bucket='pending', conclusion='in_progress')]
        self.assertEqual([a.kind for a in d.tick()], ['revoke'])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        ((comment, record),) = receipts(self.gh)
        self.assertEqual(record['state'], 'superseded')
        self.assertEqual(record['reason'], 'Readiness sospesa')
        self.assertEqual({k: record[k] for k in ('digest', 'promotion', 'head')},
                         {k: first[k] for k in ('digest', 'promotion', 'head')})
        self.assertTrue(comment['body'].startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        self.assertIn('> **Ricevuta superata** il ', comment['body'])
        # The CI passes again: the recorded approval is promoted again, with a new receipt.
        self.gh.details = [dict(c) for c in GREEN]
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        old, new = receipts(self.gh)
        self.assertEqual((old[1]['promotion'], old[1]['state']), (1, 'superseded'))
        self.assertEqual((new[1]['promotion'], new[1]['state']), (2, 'current'))
        self.assertIn('Promozione n. 2', new[0]['body'])

    def test_a_stale_policy_receipt_supersedes_the_receipt(self):
        d = self.make()
        d.tick()
        snap = d.snapshot()
        (current,) = snap.prs
        d._approvals_match(current, [dict(current.reviews[-1], receipt=None)],
                           d._receipt(current), [])
        ((_, record),) = receipts(self.gh)
        self.assertEqual(record['state'], 'superseded')
        self.assertIn('ricevuta di policy', record['reason'])

    def test_only_the_dispatchers_receipts_are_read(self):
        d = self.make()
        d.tick()
        ((comment, record),) = receipts(self.gh)
        forged = dict(comment, id=9999, author={'login': 'stranger'})
        self.gh.thread[100].append(forged)
        self.gh.edited.clear()
        d._supersede_receipts(100, 'test')
        self.assertEqual(self.gh.edited, [comment['id']])  # the forged copy is left alone

    def test_the_round_limit_after_the_promotion_supersedes_the_receipt(self):
        # Promoted at the last round available, then the CI of the approved head fails: the
        # failure joins that round (#359), which no longer approves and has no round left for
        # a fix. The PR goes to the operator and its receipt no longer holds.
        d = self.make()
        d.cfg = dataclasses.replace(CFG, max_review_rounds=1)
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.gh.details = [dict(GREEN[0], bucket='fail', conclusion='failure')]
        self.assertEqual([a.kind for a in d.tick()], ['ci_failed'])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertIn('round_limit', self.gh.comments[-1][1])
        ((comment, record),) = receipts(self.gh)
        self.assertEqual(record['state'], 'superseded')
        self.assertIn('il round 1 non approva', record['reason'])
        self.assertIn('> **Ricevuta superata** il ', comment['body'])

    def test_an_escalation_that_keeps_the_readiness_leaves_the_receipt(self):
        d = self.make()
        d.tick()
        d._escalate(100, 'altro', remove=['agent:review'], cls='failure')
        ((_, record),) = receipts(self.gh)
        self.assertEqual(record['state'], 'current')

    def test_a_level_zero_promotion_writes_its_receipt(self):
        from test_devloop import pr
        from test_devloop_ledger import LedgerGH
        from test_devloop_zero import GRANT, SUCCESS, ZCFG, ZERO_LABELS, changes
        self.gh = LedgerGH()
        ws = FakeWS()
        ws.diff = changes('docs/a.txt')
        self.gh.zero_events, self.gh.runs = list(GRANT), list(SUCCESS)
        self.gh.labels[100] = set(ZERO_LABELS)
        d = Dispatcher(ZCFG, self.gh, ws, FakeRunner(), log=lambda m: None,
                       ledger=Ledger(self.gh))
        d._do_review(pr(labels=ZERO_LABELS))
        self.assertEqual(self.gh.labels[100], {'reviews:0', 'agent:ready-to-merge'})
        ((comment, record),) = receipts(self.gh)
        self.assertEqual((record['level'], record['head'], record['state']), (0, SHA1, 'current'))
        self.assertNotIn('ci', record['missing'])  # the checks the promotion read, round none
        self.assertIn('Nessun round di review (livello 0).', comment['body'])
        self.assertIn('Livello **0**.', comment['body'])

    def test_the_receipt_has_the_budget_section_only_with_the_budget_on(self):
        rule = (('model', 'unmetered_quota'), ('reason', 'the turn caps bound it'))
        on = BudgetPolicy(
            enabled=True, turns=10, phase_turns=(('implementation', 10),), wall_seconds=86400,
            role_turns=tuple((role, 3) for role in ('implement', 'fix', 'review', 'sync')),
            metered_usd=1.0, cli_estimate_usd=5.0,
            usage=tuple((key, rule) for key, agent in CFG.agents.items()
                        if agent.kind != 'claude'))
        bodies = {}
        for cfg in (CFG, dataclasses.replace(CFG, budget=on)):
            with self.subTest(on=cfg.budget.enabled):
                d = self.make()
                d.cfg = cfg
                story = self.ledger.journal.story(CFG.repo, 7, 'h1')['story']
                self.seed_issue()
                d.tick()
                self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'}, self.logs)
                ((comment, record),) = receipts(self.gh)
                bodies[cfg.budget.enabled] = comment['body']
        self.assertNotIn('### Budget della storia', bodies[False])
        body = bodies[True]
        self.assertIn('### Budget della storia', body)
        self.assertIn(f'Storia `{story}` (radice #7)', body)
        # The review turn, booked in the envelope; the implementation was not.
        self.assertIn('| Turni | 1 | 0 | 9 | 10 |', body)
        self.assertIn('| Turni `review` | 1 | 0 | 2 | 3 |', body)
        self.assertIn('| `metered_usd` | non attestato (turni senza valore attestato: 1) |',
                      body)

    def test_without_a_ledger_no_receipt_is_written(self):
        gh = ReceiptGH()
        gh.labels[100] = {'agent:review'}
        Dispatcher(CFG, gh, FakeWS(), FakeRunner(raw_review()), log=lambda m: None).tick()
        self.assertEqual(gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(receipts(gh), [])


class ReceiptContentTests(unittest.TestCase):
    def content(self, **kw):
        policy = {'pr': 100, 'base': 'c' * 40, 'head': SHA1, 'policy': 'p' * 64, 'level': 1,
                  'reasons': [{'kind': 'default', 'level': 1}], 'reviewers': [REV],
                  'profiles': []}
        args = dict(pr=100, issue=7, head=SHA1, promotion=1, policy=policy, request=None,
                    issue_records={'turns': [], 'verifications': []},
                    pr_records={'turns': [], 'verifications': [], 'events': []}, reviews=[],
                    closures=[], approved_round=1, criteria=[], acceptance_map=[])
        args.update(kw)
        return receipt.build(**args)

    def test_contradictions(self):
        ci = {'type': 'event', 'event': 'ci_round', 'head': SHA1, 'state': 'fail', 'round': 1,
              'at': 't'}
        cases = {
            'head': (dict(), 'b' * 40, 'non coincidono'),
            'ci': (dict(pr_records={'turns': [], 'verifications': [], 'events': [ci]}), SHA1,
                   'come `fail`'),
            'request': (dict(request={'issue': 8}), SHA1, 'riguarda l\'issue #8'),
        }
        for name, (kw, approved, expected) in cases.items():
            with self.subTest(name):
                (found,) = receipt.contradictions(self.content(**kw), implementer=IMPL,
                                                  approved_head=approved)
                self.assertIn(expected, found)
        self.assertEqual(receipt.contradictions(self.content(), implementer=IMPL,
                                                approved_head=SHA1), [])

    def test_a_ci_of_another_round_or_head_is_not_the_approved_one(self):
        other = {'type': 'event', 'event': 'ci_round', 'head': SHA1, 'state': 'fail', 'round': 2,
                 'at': 't'}
        content = self.content(pr_records={'turns': [], 'verifications': [], 'events': [other]})
        self.assertIsNone(content['verifications']['ci'])
        self.assertIn('ci', content['missing'])
        self.assertEqual(receipt.contradictions(content, implementer=IMPL, approved_head=SHA1),
                         [])

    def test_the_marker_is_validated_on_read(self):
        content = self.content()
        body = receipt.render(content)
        record = markdown.read_marker(body, 'receipt')
        self.assertEqual(record['state'], 'current')
        for broken in (dict(record, state='maybe'), dict(record, head=''),
                       dict(record, promotion=0), {k: v for k, v in record.items()
                                                   if k != 'digest'}):
            self.assertIsNone(markdown.read_marker(
                markdown.publishable('x', 'receipt', broken), 'receipt'))
        # Another kind of marker is not a receipt.
        self.assertIsNone(markdown.read_marker(markdown.publishable('x', 'policy', record),
                                               'receipt'))

    def test_findings_name_who_closed_them_without_their_text(self):
        from devloop.review import ci_failure_review, normalize_review, open_findings
        from test_devloop import finding
        first = normalize_review(raw_review('request_changes', blocking=[finding('secret')]),
                                 previous_open=[], round_no=1, max_rounds=5, head_sha=SHA1,
                                 ci='pass', reviewer=REV)
        ci = ci_failure_review(failed_checks=['Unit Tests'], log_tail='', round_no=2,
                               max_rounds=5, head_sha=SHA1, previous_open=open_findings([first]))
        reviews = [first, ci]
        third = normalize_review(
            raw_review(previous=[{'id': 'R1-1', 'status': 'resolved', 'note': 'ok'}]),
            previous_open=open_findings(reviews), round_no=3, max_rounds=5, head_sha=SHA1,
            ci='pass', reviewer=REV)
        closures = [{'key': '2/ci/R2-1', 'status': 'resolved', 'by': 'ci', 'author': 'ci',
                     'reason': 'CI passata', 'head_sha': SHA1}]
        content = self.content(reviews=[*reviews, third], closures=closures, approved_round=3)
        self.assertEqual([(f['key'], f['status'], f['by']) for f in content['findings']],
                         [(f'1/{REV}/R1-1', 'resolved', REV), ('2/ci/R2-1', 'resolved', 'ci')])
        body = receipt.render(content)
        self.assertIn(f'| `1/{REV}/R1-1` | risolto | `{REV}`, nel round 3 |', body)
        self.assertIn('| `2/ci/R2-1` | risolto | `ci` (`ci`) |', body)
        self.assertEqual(content['totals']['rounds'], 3)
        self.assertNotIn('secret', body)

    def test_the_final_map_keeps_its_references_and_reasons(self):
        def entry(tests, status='covered', reason=''):
            return {'criterion': 'c', 'tests': tests, 'status': status, 'reason': reason}
        one = [entry([{'file': 'tests/a.py', 'name': 'test_x'}]),
               entry([], 'not_covered', 'manual only')]
        other = [entry([{'file': 'tests/b.py', 'name': 'test_y'}]),
                 entry([], 'not_covered', 'out of scope')]
        maps = {}
        for name, acceptance_map in (('one', one), ('other', other)):
            content = self.content(criteria=['A', 'B'], acceptance_map=acceptance_map)
            maps[name] = (content, receipt.render(content))
        self.assertEqual(maps['one'][0]['acceptance']['map'],
                         [{'status': 'covered',
                           'tests': [{'file': 'tests/a.py', 'name': 'test_x'}], 'reason': ''},
                          {'status': 'not_covered', 'tests': [], 'reason': 'manual only'}])
        self.assertIn('| 1 | A | coperto | `tests/a.py`: `test_x` | — |', maps['one'][1])
        self.assertIn('| 2 | B | non coperto | — | `manual only` |', maps['one'][1])
        self.assertIn('`tests/b.py`: `test_y`', maps['other'][1])
        self.assertIn('`out of scope`', maps['other'][1])
        self.assertNotEqual(receipt.record(maps['one'][0])['digest'],
                            receipt.record(maps['other'][0])['digest'])

    def test_agent_text_of_the_map_renders_as_inert_code(self):
        hostile = 'a `b` ``c`` | @someone [x](http://e) <img src=x>\nline'
        cell = receipt._literal(hostile)
        self.assertEqual(cell, '```a `b` ``c`` \\| @someone [x](http://e) <img src=x> line```')
        self.assertEqual(receipt._literal('`x`'), '`` `x` ``')
        self.assertEqual(receipt._literal('  '), '—')

    def test_the_digest_binds_the_content(self):
        self.assertNotEqual(receipt.record(self.content())['digest'],
                            receipt.record(self.content(promotion=2))['digest'])

    def test_the_budget_section_only_with_the_budget_on(self):
        # Off (#393): no key, the same content and digest as before, no section.
        off = self.content()
        self.assertNotIn('budget', off)
        self.assertEqual(off, self.content(budget=None))
        self.assertNotIn('### Budget della storia', receipt.section(off))
        self.assertNotIn('Budget', receipt.section(off))
        # On: the state of the story's budget, a dollar value no turn reports as such.
        policy = BudgetPolicy(enabled=True, turns=10, role_turns=(('implement', 3),),
                              phase_turns=(('implementation', 10),), wall_seconds=3600,
                              metered_usd=1.0, cli_estimate_usd=5.0)
        row = {'role': 'implement', 'phase': 'implementation', 'engine': 'codex',
               'status': 'returned', 'usage': {'metered_usd': None, 'cli_estimate_usd': None},
               'usage_model': 'unmetered_quota'}
        current = {'started_at': 1_800_000_000, 'deadline_at': 1_800_003_600,
                   'invocations': {'t1': row}}
        found = budget.view(policy, {'story': 'story-1-ab', 'issue': 7}, current,
                            1_800_000_600)
        on = self.content(budget=found)
        self.assertEqual(on['budget'], found)
        self.assertNotEqual(receipt.digest(on), receipt.digest(off))
        text = receipt.section(on)
        self.assertIn('### Budget della storia', text)
        self.assertIn('Storia `story-1-ab` (radice #7), stato del journal al '
                      '2027-01-15T08:10:00Z.', text)
        self.assertIn('| Turni | 1 | 0 | 9 | 10 |', text)
        self.assertIn('| Turni `implement` | 1 | 0 | 2 | 3 |', text)
        self.assertIn('| Fase `implementation` | 1 | 0 | 9 | 10 |', text)
        self.assertIn('| `metered_usd` | non attestato (turni senza valore attestato: 1) | '
                      '0.00 USD | non attestato | 1.00 USD |', text)
        self.assertIn('| `cli_estimate_usd` | non attestato (turni senza valore attestato: 1) | '
                      '0.00 USD | non attestato | 5.00 USD |', text)
        self.assertNotIn('| `metered_usd` | 0', text)
        self.assertIn('| Tempo (s) | 600 | — | 3000 | 3600 |', text)
        # On, for an issue of no story.
        text = receipt.section(self.content(budget={'story': None}))
        self.assertIn('L\'issue non appartiene a nessuna storia del journal', text)

    def test_superseded_keeps_the_text_and_marks_the_marker(self):
        content = self.content()
        body = receipt.render(content)
        marked = receipt.superseded(body, markdown.read_marker(body, 'receipt'), 'nuovo head',
                                    '2026-11-18T10:00:00Z')
        record = markdown.read_marker(marked, 'receipt')
        self.assertEqual((record['state'], record['reason'], record['superseded_at']),
                         ('superseded', 'nuovo head', '2026-11-18T10:00:00Z'))
        self.assertIn('## Ricevuta della PR #100', marked)
        self.assertLess(marked.index('Ricevuta superata'), marked.index('## Ricevuta della PR'))


RECORDS = ('policy', 'receipt', 'merged')
STRICT = dataclasses.replace(CFG, merge=MergePolicy(mode='strict'))


def records_of(body):
    """The records of the promotion and of the merge ``body`` carries, by kind."""
    return {k: r for k in RECORDS if (r := markdown.read_marker(body, k)) is not None}


class PromotionCommentTests(unittest.TestCase):
    """One comment for the promotion, its receipt and the merge (#338)."""

    def make(self, cfg=STRICT):
        self.gh = ReceiptGH()
        self.gh.labels[100] = {'agent:review'}
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(*[raw_review()] * 3),
                          log=lambda m: None, ledger=Ledger(self.gh))

    def carrying(self):
        """The comments of PR #100 that carry a record of the promotion or of the merge."""
        return [c for c in self.gh.thread[100] if records_of(c['body'])]

    def test_a_promotion_merged_by_devloop_is_one_comment_with_three_records(self):
        auto = dataclasses.replace(CFG, merge=MergePolicy(mode='auto'), supervisor='agy')
        for cfg in (STRICT, auto):
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg)
                d.tick()
                self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
                (promotion,) = self.carrying()
                self.assertEqual(set(records_of(promotion['body'])), {'policy', 'receipt'})
                written, edits = len(self.gh.thread[100]), len(self.gh.edited)
                self.assertEqual([a.kind for a in d.tick()], ['merge'])
                self.assertEqual(self.gh.merged, [(100, SHA1)])
                # No new comment: the merge edited the one of the promotion.
                self.assertEqual(len(self.gh.thread[100]), written)
                self.assertEqual(self.gh.edited[edits:], [promotion['id']])
                (comment,) = self.carrying()
                self.assertEqual(comment['id'], promotion['id'])
                found = records_of(comment['body'])
                self.assertEqual(found['policy']['event'], 'promoted')
                self.assertEqual(found['policy']['receipt']['head'], SHA1)
                self.assertEqual((found['receipt']['promotion'], found['receipt']['state'],
                                  found['receipt']['head']), (1, 'current', SHA1))
                self.assertEqual({k: found['merged'][k] for k in ('head', 'level', 'mode')},
                                 {'head': SHA1, 'level': 1, 'mode': cfg.merge.mode})
                self.assertEqual(found['merged']['receipt'], found['policy']['receipt'])
                body = comment['body']
                self.assertEqual(body.count('> **Bot:** devloop dispatcher'), 1)
                titles = ['## Pronta per il merge', '## Ricevuta della PR #100',
                          '## Mergiata da devloop']
                self.assertEqual([body.index(t) for t in titles],
                                 sorted(body.index(t) for t in titles))
                # The policy receipt of the merge is the promotion's: it is not repeated.
                self.assertNotIn('Ricevuta di policy ricalcolata per il merge', body)
                # The dispatcher's own readers find the three records in that comment.
                thread = self.gh.pr_comments(100)
                self.assertEqual(
                    [p for p, _ in d._dispatcher_records(thread, 'policy', 'promoted')]
                    + [p for p, _ in d._dispatcher_records(thread, 'receipt')]
                    + [p for p, _ in d._dispatcher_records(thread, 'merged')],
                    [thread.index(next(c for c in thread if c['id'] == comment['id']))] * 3)

    def test_a_promotion_without_a_merge_stays_promotion_and_receipt(self):
        for case in ('operator', 'hold'):
            with self.subTest(case=case):
                d = self.make(CFG if case == 'operator' else STRICT)
                d.tick()
                (promotion,) = self.carrying()
                if case == 'hold':
                    self.gh.labels[100].add('agent:merge-hold')
                kinds = [a.kind for a in d.tick()]
                self.assertEqual(kinds, ['operator_merge'] if case == 'operator' else [])
                self.assertEqual(getattr(self.gh, 'merged', []), [])
                (comment,) = self.carrying()
                self.assertEqual(comment, promotion)  # neither edited nor followed
                self.assertEqual(set(records_of(comment['body'])), {'policy', 'receipt'})
                self.assertNotIn('Mergiata da devloop', comment['body'])

    def test_a_merge_after_a_new_promotion_edits_only_the_valid_one(self):
        d = self.make()
        self.gh.details = [dict(GREEN[0], bucket='pending', conclusion='in_progress')]
        d.tick()
        # The approval waits for the CI; it passes, the PR is promoted, then the CI runs again.
        self.gh.details = [dict(c) for c in GREEN]
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.gh.details = [dict(GREEN[0], bucket='pending', conclusion='in_progress')]
        self.assertEqual([a.kind for a in d.tick()], ['revoke'])
        self.gh.details = [dict(c) for c in GREEN]
        d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual([a.kind for a in d.tick()], ['merge'])
        first, second = self.carrying()
        self.assertEqual(set(records_of(first['body'])), {'policy', 'receipt'})
        self.assertEqual(records_of(first['body'])['receipt']['state'], 'superseded')
        self.assertNotIn('Mergiata da devloop', first['body'])
        found = records_of(second['body'])
        self.assertEqual(set(found), set(RECORDS))
        self.assertEqual((found['receipt']['promotion'], found['receipt']['state']),
                         (2, 'current'))

    def test_a_promotion_written_before_338_gets_its_merge_record(self):
        # Promoted in the old form: the receipt in a comment of its own, after the promotion.
        d = self.make()
        d.tick()
        (combined,) = self.carrying()
        text, _ = markdown.split_markers(combined['body'])
        found = records_of(combined['body'])
        title = text.index('## Ricevuta della PR')
        old_policy = markdown.publishable(text[:title], 'policy', found['policy'])
        old_receipt = markdown.publishable(markdown.dispatcher_header() + '\n' + text[title:],
                                           'receipt', found['receipt'])
        thread = self.gh.thread[100]
        at = thread.index(combined)
        thread[at:at + 1] = [dict(combined, body=old_policy),
                             dict(combined, id=combined['id'] + 500, body=old_receipt)]
        snap = d.snapshot().prs[0]
        self.assertEqual((snap.promotions, snap.labels), (1, {'agent:ready-to-merge'}))
        self.assertEqual([a.kind for a in d.tick()], ['merge'])
        promotion, receipt_comment = self.carrying()
        self.assertEqual(set(records_of(promotion['body'])), {'policy', 'merged'})
        self.assertIn('## Mergiata da devloop', promotion['body'])
        self.assertEqual(records_of(receipt_comment['body']),
                         {'receipt': found['receipt']})

    def test_without_a_promotion_comment_the_merge_writes_its_own(self):
        # A ready PR whose promotion comment cannot be edited: the record of #172 as before.
        d = self.make()
        d.tick()
        (promotion,) = self.carrying()
        self.gh.fail_edits = lambda body: markdown.read_marker(body, 'merged') is not None
        self.assertEqual([a.kind for a in d.tick()], ['merge'])
        promotion_after, merged = self.carrying()
        self.assertEqual(promotion_after, promotion)
        self.assertEqual(set(records_of(merged['body'])), {'merged'})
        self.assertTrue(merged['body'].startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        self.assertIn('## Mergiata da devloop', merged['body'])


class MarkerBlockTests(unittest.TestCase):
    """The records of a comment are the block of marker lines that ends it (#338)."""

    POLICY = {'event': 'promoted', 'receipt': {
        'pr': 100, 'base': 'b' * 40, 'head': SHA1, 'policy': 'p', 'level': 1,
        'reviewers': ['codex'], 'reasons': []}}

    def test_each_record_of_the_block_is_read_and_the_old_form_still_is(self):
        merged = {'head': SHA1, 'level': 1, 'mode': 'strict', 'receipt': self.POLICY['receipt']}
        content = ReceiptContentTests().content()
        record = receipt.record(content)
        combined = markdown.publishable('## Pronta per il merge', 'policy', self.POLICY,
                                        records=[('receipt', record), ('merged', merged)])
        self.assertEqual(records_of(combined),
                         {'policy': self.POLICY, 'receipt': record, 'merged': merged})
        # The forms written before #338: one record per comment, as its last line.
        for body, kind, expected in (
                (markdown.publishable('x', 'policy', self.POLICY), 'policy', self.POLICY),
                (receipt.render(content), 'receipt', record),
                (markdown.publishable('x', 'merged', merged), 'merged', merged)):
            self.assertEqual(records_of(body), {kind: expected})

    def test_only_the_final_block_counts_and_once_per_kind(self):
        line = markdown.marker('policy', self.POLICY)
        merged = markdown.marker('merged', {'head': SHA1, 'level': 1, 'mode': 'strict',
                                            'receipt': self.POLICY['receipt']})
        self.assertIsNone(markdown.read_marker(f'{line}\ntext\n{merged}', 'policy'))
        self.assertIsNone(markdown.read_marker(f'text\n{line}\n\n{merged}', 'policy'))
        self.assertIsNone(markdown.read_marker(f'text\n{line}\n{line}', 'policy'))
        self.assertEqual(markdown.read_marker(f'text\n{line}\n{merged}', 'policy'), self.POLICY)
        self.assertIsNone(markdown.read_marker(f'text\n> {line}\n{merged}', 'policy'))

    def test_with_record_adds_or_replaces_and_keeps_the_others(self):
        body = markdown.publishable('text', 'policy', self.POLICY)
        merged = {'head': SHA1, 'level': 1, 'mode': 'auto', 'receipt': self.POLICY['receipt']}
        added = markdown.with_record(body, 'merged', merged, text='text\n\nmore <!-- x')
        self.assertEqual(records_of(added), {'policy': self.POLICY, 'merged': merged})
        self.assertIn('more &lt;!-- x', added)
        replaced = markdown.with_record(added, 'merged', dict(merged, mode='strict'))
        self.assertEqual(records_of(replaced)['merged']['mode'], 'strict')
        self.assertEqual(markdown.split_markers(replaced)[0],
                         markdown.split_markers(added)[0])
        self.assertEqual(len(markdown.split_markers(replaced)[1]), 2)

    def test_superseding_the_receipt_of_a_promotion_keeps_its_policy_record(self):
        content = ReceiptContentTests().content()
        body = markdown.publishable('## Pronta per il merge\n\n' + receipt.section(content),
                                    'policy', self.POLICY,
                                    records=[('receipt', receipt.record(content))])
        marked = receipt.superseded(body, markdown.read_marker(body, 'receipt'), 'nuovo head',
                                    '2026-11-18T10:00:00Z')
        found = records_of(marked)
        self.assertEqual(found['policy'], self.POLICY)
        self.assertEqual(found['receipt']['state'], 'superseded')
        self.assertLess(marked.index('Ricevuta superata'), marked.index('## Pronta per il merge'))


class ReceiptFakeTests(unittest.TestCase):
    def test_the_plain_fake_has_no_comment_ids_and_receipts_need_them(self):
        # FakeGH comments carry no id: superseding cannot edit them, and logs it.
        gh = FakeGH()
        gh.comments.append((100, receipt.render(ReceiptContentTests().content())))
        logs = []
        d = Dispatcher(CFG, gh, FakeWS(), FakeRunner(), log=logs.append, ledger=Ledger(gh))
        from test_devloop import pr
        d._supersede_receipts(pr().number, 'test')
        self.assertEqual(gh.comment_edits, [])
        self.assertTrue(any('not marked superseded' in line for line in logs))


if __name__ == '__main__':
    unittest.main()
