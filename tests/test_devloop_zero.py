"""devloop: level 0, no review, only by a trusted user's label, inside an allowlist and with the
expected CI green (#107, part 2 of #66)."""

import dataclasses
import tomllib
import unittest
from types import SimpleNamespace

from devloop import config, markdown, policy
from devloop.dispatcher import Dispatcher, ensure_labels
from devloop.github import GitHub
from devloop.planning import Action, Snapshot, plan
from helpers.devloop_fakes import FakeGH, SnapshotGH
from test_devloop import (BASE, CFG, SHA1, SHA2, SHA3, FakeRunner, FakeWS, finding, norm, pr,
                          raw_review, receipt)

POLICY = CFG.review_policy
DATA = tomllib.loads(config.DEFAULT_CONFIG.read_text())
APP = 15368
ZPOLICY = dataclasses.replace(POLICY, zero_allowlist=('docs/', 'tests/', 'docs/guide.md'),
                              zero_checks=(('unittest', APP),))
ZCFG = dataclasses.replace(CFG, review_policy=ZPOLICY)
AT = '2026-11-16T10:00:00Z'
GRANT = [{'event': 'labeled', 'by': 'brushtyler', 'at': AT}]
SUCCESS = [{'id': 1, 'name': 'unittest', 'app_id': APP, 'status': 'completed',
            'conclusion': 'success'}]
ZERO_LABELS = ('agent:review', 'reviews:0')


def changes(*paths, links=None):
    return [{'path': p, 'links': list((links or {}).get(p, ()))} for p in paths]


def published(gh, number=100):
    return [body for n, body in gh.comments if n == number]


def records(gh, kind):
    return [r for r in (markdown.read_marker(b, kind) for b in published(gh)) if r]


def zero_reason(receipt_):
    found = [r for r in receipt_['reasons'] if r['kind'] == 'zero']
    return found[0] if found else None


class ZeroConfigTests(unittest.TestCase):
    def table(self, **zero):
        base = {'label': 'reviews:0', 'allowlist': ['docs/', 'tests/x.py'],
                'checks': [{'name': 'unittest', 'app_id': APP}]}
        return dict(DATA, review_policy=dict(DATA['review_policy'], zero=dict(base, **zero)))

    def test_default_is_off_and_keeps_the_version_of_part_1(self):
        self.assertFalse(POLICY.zero_enabled())
        self.assertEqual((POLICY.zero_label, POLICY.zero_allowlist, POLICY.zero_checks),
                         ('reviews:0', (), ()))
        # Checks without an allowlist, or an allowlist without checks: still off, same version.
        for half in ({'zero_checks': (('unittest', APP),)}, {'zero_allowlist': ('docs/',)}):
            with self.subTest(half=half):
                off = dataclasses.replace(POLICY, **half)
                self.assertFalse(off.zero_enabled())
                self.assertEqual(off.version(), POLICY.version())
        self.assertNotEqual(ZPOLICY.version(), POLICY.version())
        wider = dataclasses.replace(ZPOLICY, zero_allowlist=ZPOLICY.zero_allowlist + ('x/',))
        self.assertNotEqual(wider.version(), ZPOLICY.version())

    def test_parse_the_zero_table(self):
        parsed = config.parse_config(self.table()).review_policy
        self.assertTrue(parsed.zero_enabled())
        self.assertEqual(parsed.zero_allowlist, ('docs/', 'tests/x.py'))
        self.assertEqual(parsed.zero_checks, (('unittest', APP),))

    def test_invalid_zero_tables_stop_devloop(self):
        for change in ({'label': ''}, {'label': 'reviews:2'}, {'allowlist': 'docs/'},
                       {'allowlist': ['/']}, {'allowlist': ['']}, {'allowlist': ['../x']},
                       {'allowlist': ['./docs']}, {'allowlist': ['docs/*']},
                       {'allowlist': ['/etc/']}, {'allowlist': ['a//b']},
                       {'checks': 'unittest'}, {'checks': [{'name': 'unittest'}]},
                       {'checks': [{'name': 'unittest', 'app_id': '1'}]},
                       {'checks': [{'name': 'unittest', 'app_id': True}]},
                       {'checks': [{'name': '', 'app_id': 1}]},
                       {'checks': [{'name': 'u', 'app_id': 1, 'status': 'x'}]},
                       {'checks': [{'name': 'u', 'app_id': 1}, {'name': 'u', 'app_id': 1}]}):
            with self.subTest(change=change), self.assertRaises(config.ConfigError):
                config.parse_config(self.table(**change))
        with self.assertRaises(config.ConfigError):
            config.parse_config(dict(DATA, review_policy=dict(DATA['review_policy'], zero='x')))

    def test_setup_creates_the_zero_label_only_when_enabled(self):
        created = []
        gh = SimpleNamespace(ensure_label=lambda name, *style: created.append(name),
                             repo_labels=lambda: [])
        ensure_labels(gh, CFG.labels, POLICY)
        self.assertNotIn('reviews:0', created)
        ensure_labels(gh, CFG.labels, ZPOLICY)
        self.assertIn('reviews:0', created)


class ZeroLevelTests(unittest.TestCase):
    def level(self, paths=('docs/a.txt',), links=None, events=GRANT, labelers=(), previous=0,
              refused=None, pol=ZPOLICY):
        return policy.evaluate(pol, pr=100, base=BASE, head=SHA1,
                               changes=changes(*paths, links=links), labelers=list(labelers),
                               trusted_users=CFG.trusted_users, previous_level=previous,
                               select=lambda level: ['codex'] if level == 1 else [],
                               zero_events=list(events), refused=refused)

    def test_trusted_label_inside_the_allowlist_gives_level_zero(self):
        result = self.level(('docs/a.txt', 'docs/guide.md', 'tests/test_x.py'))
        self.assertEqual((result['level'], result['reviewers'], result['profiles']), (0, [], []))
        self.assertEqual(zero_reason(result), {
            'kind': 'zero', 'level': 0, 'label': 'reviews:0', 'by': 'brushtyler', 'at': AT,
            'applied': True, 'why': [], 'outside': [], 'count': 0})
        self.assertTrue(policy.valid_receipt(result))
        text = policy.describe(result)
        self.assertIn('Label `reviews:0` messa da `brushtyler`', text)
        self.assertIn('nessuno (livello 0)', text)

    def test_untrusted_or_removed_label_is_ignored(self):
        for events in ([{'event': 'labeled', 'by': 'stranger', 'at': AT}],
                       # Re-added by someone untrusted: the latest event decides.
                       GRANT + [{'event': 'unlabeled', 'by': 'stranger', 'at': AT},
                                {'event': 'labeled', 'by': 'stranger', 'at': AT}],
                       GRANT + [{'event': 'unlabeled', 'by': 'brushtyler', 'at': AT}], []):
            with self.subTest(events=events):
                result = self.level(events=events)
                self.assertEqual(result['level'], 1)
                self.assertIsNone(zero_reason(result))

    def test_paths_outside_the_allowlist_keep_level_one(self):
        for path in ('x.py', 'docs/other.md', 'README.md', 'docs/AGENTS.md', 'tests/claude.md',
                     'docs/sub/CLAUDE.md', 'docsx/a.txt'):
            with self.subTest(path=path):
                result = self.level(('docs/a.txt', path))
                self.assertEqual(result['level'], 1)
                reason = zero_reason(result)
                self.assertEqual((reason['applied'], reason['why'], reason['outside']),
                                 (False, ['allowlist'], [path]))
                self.assertIn(f'`{path}`', policy.describe(result))
        # A symlink never qualifies, whatever it points to.
        result = self.level(('docs/link',), links={'docs/link': ['a.txt']})
        self.assertEqual((result['level'], zero_reason(result)['outside']), (1, ['docs/link']))

    def test_excluded_paths_stay_excluded_even_when_listed(self):
        listed = dataclasses.replace(ZPOLICY, zero_allowlist=(
            'docs/', 'devloop/prompts/', 'standards/', '.github/', 'schemas/', 'AGENTS.md',
            'docs/AGENTS.md', 'ssdlc_grader.py', 'devloop/', '.GitHub/'))
        for path in ('devloop/prompts/review.md', 'standards/x.txt', '.github/workflows/t.yml',
                     'schemas/recovery-grader-baseline.json', 'AGENTS.md', 'docs/AGENTS.md',
                     'ssdlc_grader.py', 'devloop/policy.py', '.GitHub/x.yml'):
            with self.subTest(path=path):
                result = self.level((path,), pol=listed)
                self.assertGreaterEqual(result['level'], 1)
                self.assertFalse(zero_reason(result)['applied'])
                self.assertIn(path, zero_reason(result)['outside'])

    def test_markdown_needs_its_exact_path(self):
        self.assertEqual(self.level(('docs/guide.md',))['level'], 0)
        for path in ('docs/guide2.md', 'docs/notes.markdown', 'docs/page.MD', 'docs/x.mdx'):
            with self.subTest(path=path):
                self.assertEqual(self.level((path,))['level'], 1)

    def test_paths_are_compared_exactly_as_git_names_them(self):
        # Leading or trailing spaces make another file: never trimmed into an allowed entry.
        for path in (' docs/a.txt', 'docs/guide.md ', ' docs/guide.md', 'docs/AGENTS.md ',
                     ' .github/x.yml', 'docs/x.md '):
            with self.subTest(path=path):
                result = self.level(('docs/a.txt', path))
                self.assertGreaterEqual(result['level'], 1)
                self.assertEqual(zero_reason(result)['outside'], [path])
        # Spaces inside a name under a listed directory are just part of the name.
        for path in ('docs/a b.txt', 'docs/a.txt '):
            with self.subTest(path=path):
                self.assertEqual(self.level((path,))['level'], 0)

    def test_level_two_prevails(self):
        # The level-2 label, a level 2 recorded earlier, a sensitive path, a default of 2.
        for kwargs in ({'labelers': ['brushtyler']}, {'previous': 2},
                       {'paths': ('docs/a.txt', 'devloop/x.py')},
                       {'pol': dataclasses.replace(ZPOLICY, default_level=2)}):
            with self.subTest(kwargs=kwargs):
                result = self.level(**kwargs)
                self.assertEqual(result['level'], 2)
                self.assertIn('level', zero_reason(result)['why'])
        # A level 1 recorded earlier is what the label lowers.
        self.assertEqual(self.level(previous=1)['level'], 0)

    def test_a_refused_label_needs_a_new_one(self):
        refused = self.level(refused=AT)
        self.assertEqual(refused['level'], 1)
        self.assertEqual(zero_reason(refused)['why'], ['refused'])
        self.assertEqual(policy.zero_refused([{'receipt': refused}]), AT)
        # The refusal comes from a receipt that left the allowlist, and it persists.
        wide = self.level(('docs/a.txt', 'x.py'))
        self.assertEqual(policy.zero_refused([{'receipt': wide}, {'receipt': self.level()}]), AT)
        newer = GRANT + [{'event': 'unlabeled', 'by': 'brushtyler', 'at': '2026-11-16T11:00:00Z'},
                         {'event': 'labeled', 'by': 'brushtyler', 'at': '2026-11-16T11:00:01Z'}]
        self.assertEqual(self.level(events=newer, refused=AT)['level'], 0)

    def test_disabled_level_zero_is_part_1(self):
        for pol in (POLICY, dataclasses.replace(POLICY, zero_checks=(('unittest', APP),)),
                    dataclasses.replace(POLICY, zero_allowlist=('docs/',))):
            with self.subTest(pol=pol):
                result = self.level(pol=pol, refused=AT)
                self.assertEqual(result, policy.evaluate(
                    pol, pr=100, base=BASE, head=SHA1, changes=changes('docs/a.txt'),
                    labelers=[], trusted_users=CFG.trusted_users, previous_level=0,
                    select=lambda level: ['codex'] if level == 1 else []))
                self.assertEqual(result['level'], 1)


class ExpectedChecksTests(unittest.TestCase):
    def outcome(self, runs, expected=(('unittest', APP),)):
        states = policy.check_states(expected, runs)
        return [s['state'] for s in states], policy.check_outcome(states)

    def run_(self, id_=1, status='completed', conclusion='success', name='unittest', app=APP):
        return {'id': id_, 'name': name, 'app_id': app, 'status': status,
                'conclusion': conclusion}

    def test_only_success_counts(self):
        self.assertEqual(self.outcome([self.run_()]), (['success'], 'success'))
        self.assertEqual(self.outcome([]), (['missing'], 'failed'))
        self.assertEqual(self.outcome([self.run_(status='in_progress', conclusion=None)]),
                         (['pending'], 'pending'))
        for conclusion in ('skipped', 'neutral', 'cancelled', 'failure', 'stale', None):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(self.outcome([self.run_(conclusion=conclusion)])[1], 'failed')
        # The exact name and the app both count.
        self.assertEqual(self.outcome([self.run_(app=1)]), (['missing'], 'failed'))
        self.assertEqual(self.outcome([self.run_(name='unittest (3.12)')]),
                         (['missing'], 'failed'))
        # No expected check is never a success.
        self.assertEqual(self.outcome([self.run_()], expected=()), ([], 'failed'))

    def test_the_latest_run_decides(self):
        self.assertEqual(self.outcome([self.run_(2, 'queued', None), self.run_(1)]),
                         (['pending'], 'pending'))
        self.assertEqual(self.outcome([self.run_(1, conclusion='failure'), self.run_(2)]),
                         (['success'], 'success'))
        self.assertEqual(self.outcome([self.run_(2, conclusion='skipped'), self.run_(1)])[1],
                         'failed')
        # A definitive failure is not waited for.
        expected = (('unittest', APP), ('lint', APP))
        self.assertEqual(self.outcome([self.run_(1, 'in_progress', None),
                                       self.run_(2, name='lint', conclusion='neutral')],
                                      expected)[1], 'failed')

    def test_standing_requests_for_changes(self):
        trusted = ('brushtyler',)

        def blocking(*reviews):
            return policy.changes_requested([{'login': w, 'state': s} for w, s in reviews],
                                            trusted)
        self.assertEqual(blocking(('brushtyler', 'CHANGES_REQUESTED')), ['brushtyler'])
        self.assertEqual(blocking(('brushtyler', 'CHANGES_REQUESTED'),
                                  ('brushtyler', 'COMMENTED')), ['brushtyler'])
        self.assertEqual(blocking(('brushtyler', 'CHANGES_REQUESTED'),
                                  ('brushtyler', 'APPROVED')), [])
        self.assertEqual(blocking(('brushtyler', 'DISMISSED')), [])
        self.assertEqual(blocking(('brushtyler', 'APPROVED'),
                                  ('brushtyler', 'CHANGES_REQUESTED')), ['brushtyler'])
        self.assertEqual(blocking(('stranger', 'CHANGES_REQUESTED')), [])
        self.assertEqual(blocking(('brushtyler', 'CHANGES_REQUESTED'),
                                  ('stranger', 'APPROVED')), ['brushtyler'])


class ZeroAdapterTests(unittest.TestCase):
    def test_reads_use_rest(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(' '.join(argv))
            out = {'events': '{"event":"labeled","by":"brushtyler","at":"t"}\n',
                   'check-runs': '{"id":1,"name":"u","app_id":2,"status":"completed",'
                                 '"conclusion":"success"}\n',
                   'reviews': '{"id":3,"login":"brushtyler","state":"APPROVED"}\n'}
            body = next((v for k, v in out.items() if k in argv[3]), '')
            return SimpleNamespace(returncode=0, stdout=body, stderr='')
        gh = GitHub('o/r', run=fake_run)
        self.assertEqual(gh.label_events(5, 'reviews:0'),
                         [{'event': 'labeled', 'by': 'brushtyler', 'at': 't'}])
        self.assertIn('repos/o/r/issues/5/events', calls[-1])
        self.assertIn('"reviews:0"', calls[-1])
        self.assertIn('unlabeled', calls[-1])
        self.assertEqual(gh.check_runs(SHA1)[0]['app_id'], 2)
        self.assertIn(f'repos/o/r/commits/{SHA1}/check-runs?filter=all', calls[-1])
        self.assertIn('.app.id', calls[-1])
        self.assertEqual(gh.pr_reviews(5), [{'id': 3, 'login': 'brushtyler', 'state': 'APPROVED'}])
        self.assertIn('repos/o/r/pulls/5/reviews', calls[-1])
        self.assertTrue(all(c.startswith('gh api --paginate') for c in calls))


class ZeroDispatcherTests(unittest.TestCase):
    def make(self, runner=None, ws=None, cfg=ZCFG, diff=('docs/a.txt',), events=GRANT,
             runs=SUCCESS):
        self.gh, self.ws = FakeGH(), ws or FakeWS()
        self.ws.diff = changes(*diff)
        self.gh.zero_events, self.gh.runs = list(events), list(runs)
        self.gh.labels[100] = set(ZERO_LABELS)
        self.runner = runner or FakeRunner()
        return Dispatcher(cfg, self.gh, self.ws, self.runner, log=lambda m: None)

    def zpr(self, **kw):
        return pr(labels=ZERO_LABELS, **kw)

    def assert_promoted_without_review(self):
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.labels[100], {'reviews:0', 'agent:ready-to-merge'})
        (record,) = records(self.gh, 'policy')
        self.assertEqual(record['event'], 'promoted')
        self.assertEqual((record['receipt']['level'], record['receipt']['reviewers']), (0, []))
        reason = zero_reason(record['receipt'])
        self.assertEqual((reason['by'], reason['at'], reason['applied']), ('brushtyler', AT, True))
        self.assertEqual(record['checks'], [{'name': 'unittest', 'app_id': APP,
                                             'state': 'success'}])
        body = published(self.gh)[-1]
        self.assertTrue(body.startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        self.assertIn('Il merge resta un\'azione dell\'operatore', body)

    def assert_refused(self, why):
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.gh.labels[100], {'reviews:0', 'agent:needs-operator'})
        record = records(self.gh, 'policy')[-1]
        self.assertEqual((record['event'], record['receipt']['level']), ('level', 0))
        self.assertIn(why, published(self.gh)[-1])

    def test_trusted_label_inside_the_allowlist_with_green_ci_needs_no_review(self):
        d = self.make(diff=('docs/a.txt', 'docs/guide.md', 'tests/test_x.py'))
        d._do_review(self.zpr())
        self.assert_promoted_without_review()

    def test_untrusted_label_is_ignored(self):
        d = self.make(FakeRunner(raw_review()),
                      events=[{'event': 'labeled', 'by': 'stranger', 'at': AT}])
        d._do_review(self.zpr())
        self.assertEqual(len(self.runner.calls), 1)
        recorded = records(self.gh, 'review')[0]['receipt']
        self.assertEqual(recorded['level'], 1)
        self.assertIsNone(zero_reason(recorded))
        self.assertEqual(records(self.gh, 'policy')[-1]['receipt']['level'], 1)

    def test_a_diff_outside_the_allowlist_gets_a_review(self):
        for path, level in (('x.py', 1), ('docs/other.md', 1), ('docs/AGENTS.md', 1),
                            ('devloop/prompts/review.md', 2)):
            with self.subTest(path=path):
                d = self.make(FakeRunner(raw_review()), diff=('docs/a.txt', path))
                d._do_review(self.zpr())
                recorded = (records(self.gh, 'review') or records(self.gh, 'policy'))[0]
                self.assertEqual(recorded['receipt']['level'], level)
                self.assertFalse(zero_reason(recorded['receipt'])['applied'])
                self.assertIn(f'`{path}`', policy.describe(recorded['receipt']))
                # Level 1: one reviewer approved; level 2: no pair in the pool, a human.
                self.assertEqual(len(self.runner.calls), 1 if level == 1 else 0)
                self.assertIn('agent:ready-to-merge' if level == 1 else 'agent:needs-operator',
                              self.gh.labels[100])
                self.assertNotIn(0, [r['receipt']['level'] for r in records(self.gh, 'policy')])

    def test_expected_checks_must_succeed(self):
        def run_(status='completed', conclusion=None):
            return [dict(SUCCESS[0], status=status, conclusion=conclusion)]
        for state, runs in (('missing', []), ('skipped', run_(conclusion='skipped')),
                            ('neutral', run_(conclusion='neutral')),
                            ('cancelled', run_(conclusion='cancelled')),
                            ('missing', [dict(SUCCESS[0], app_id=1)])):
            with self.subTest(state=state):
                d = self.make(runs=runs)
                d._do_review(self.zpr())
                self.assert_refused(f'`unittest` (app {APP}): `{state}`')
        # A check still running is waited for: nothing is published.
        d = self.make(runs=run_('in_progress'))
        d._do_review(self.zpr())
        self.assertEqual((self.gh.comments, self.gh.labels[100]), ([], set(ZERO_LABELS)))
        # So is CI as a whole, even with the expected checks green.
        d = self.make()
        self.gh.ci = 'pending'
        d._do_review(self.zpr())
        self.assertEqual(self.gh.comments, [])

    def test_a_standing_request_for_changes_blocks(self):
        d = self.make()
        self.gh.native_reviews = [{'login': 'brushtyler', 'state': 'CHANGES_REQUESTED'},
                                  {'login': 'brushtyler', 'state': 'COMMENTED'}]
        d._do_review(self.zpr())
        self.assert_refused('`REQUEST_CHANGES`')
        for native in ([{'login': 'brushtyler', 'state': 'CHANGES_REQUESTED'},
                        {'login': 'brushtyler', 'state': 'APPROVED'}],
                       [{'login': 'brushtyler', 'state': 'DISMISSED'}],
                       [{'login': 'stranger', 'state': 'CHANGES_REQUESTED'}]):
            with self.subTest(native=native):
                d = self.make()
                self.gh.native_reviews = native
                d._do_review(self.zpr())
                self.assert_promoted_without_review()

    def test_open_findings_block(self):
        d = self.make()
        d._do_review(self.zpr(reviews=[norm(raw_review('request_changes', [finding()]))]))
        self.assert_refused('restano finding bloccanti aperti')

    def test_a_recorded_approval_is_promoted_at_level_zero(self):
        # The operator lowers a PR whose level-1 approval is already recorded.
        d = self.make()
        p = self.zpr(reviews=[norm(raw_review())])
        self.assertEqual(plan(Snapshot([p], [], []), ZCFG),
                         [Action('apply_review', 100, 'approval already recorded')])
        d._do_apply_review(p)
        self.assert_promoted_without_review()

    def test_a_fix_that_widens_the_diff_returns_to_level_one_for_good(self):
        d = self.make(FakeRunner(raw_review(), raw_review()))
        # Round 1, at level 0: CI fails.
        d._do_ci_failed(self.zpr(checks='fail'))
        ci = records(self.gh, 'review')[0]
        self.assertEqual(ci['receipt']['level'], 0)
        closed = [{'key': f['key'], 'status': 'resolved', 'by': 'ci', 'author': 'ci',
                   'reason': 'green', 'head_sha': SHA2} for f in ci['blocking_findings']]
        # The fix widens the diff: level 1, a reviewer runs.
        self.ws._head, self.gh.head = SHA2, SHA2
        self.ws.diff = changes('docs/a.txt', 'x.py')
        p = self.zpr(head=SHA2, reviews=[ci])
        p.closures = closed
        d._do_review(p)
        first = records(self.gh, 'review')[-1]
        self.assertEqual(first['receipt']['level'], 1)
        self.assertEqual(zero_reason(first['receipt'])['why'], ['allowlist'])
        # A later head back inside the allowlist stays at level 1: the label was refused.
        self.ws._head, self.gh.head = SHA3, SHA3
        self.ws.diff = changes('docs/a.txt')
        p = self.zpr(head=SHA3, reviews=[ci, first])
        p.closures = closed
        d._do_review(p)
        second = records(self.gh, 'review')[-1]
        self.assertEqual(second['receipt']['level'], 1)
        self.assertEqual(zero_reason(second['receipt'])['why'], ['refused'])
        self.assertIn('serve una nuova label', policy.describe(second['receipt']))
        self.assertEqual(len(self.runner.calls), 2)
        # Only a new label of the operator brings it back to level 0.
        self.gh.zero_events = GRANT + [
            {'event': 'unlabeled', 'by': 'brushtyler', 'at': '2026-11-16T12:00:00Z'},
            {'event': 'labeled', 'by': 'brushtyler', 'at': '2026-11-16T12:00:05Z'}]
        p = self.zpr(head=SHA3, reviews=[ci, first, second])
        p.closures = closed
        p.policy = records(self.gh, 'policy')
        self.gh.labels[100] = set(ZERO_LABELS)
        d._do_apply_review(p)
        self.assertEqual(self.gh.labels[100], {'reviews:0', 'agent:ready-to-merge'})
        promoted = records(self.gh, 'policy')[-1]
        self.assertEqual((promoted['event'], promoted['receipt']['level']), ('promoted', 0))
        self.assertEqual(len(self.runner.calls), 2)

    def test_without_an_allowlist_nothing_changes(self):
        d = self.make(FakeRunner(raw_review()), cfg=CFG)
        d._do_review(self.zpr())
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual(records(self.gh, 'review')[0]['receipt'], receipt())
        self.assertEqual(getattr(self.gh, 'zero_reads', 0), 0)
        self.assertEqual(self.gh.labels[100], {'reviews:0', 'agent:ready-to-merge'})


class ZeroReadinessTests(unittest.TestCase):
    def ready_pr(self, labels=('agent:ready-to-merge', 'reviews:0'), zero_head=SHA1, **kw):
        p = pr(labels=labels, **kw)
        p.zero_head = zero_head
        p.operator_merges = [p.head_sha]  # the operator merge (#248) was already told
        return p

    def test_readiness_holds_only_for_its_head_and_labels(self):
        self.assertEqual(plan(Snapshot([self.ready_pr()], [], []), ZCFG), [])
        revoked = [Action('revoke', 100, 'the level 0 promotion no longer holds')]
        for p, cfg in ((self.ready_pr(head=SHA2), ZCFG),
                       (self.ready_pr(labels=('agent:ready-to-merge',)), ZCFG),
                       (self.ready_pr(labels=('agent:ready-to-merge', 'reviews:0', 'reviews:2')),
                        ZCFG),
                       (self.ready_pr(), CFG)):
            with self.subTest(labels=p.labels, head=p.head_sha, cfg=cfg is CFG):
                self.assertEqual(plan(Snapshot([p], [], []), cfg), revoked)
        self.assertEqual(plan(Snapshot([self.ready_pr(checks='pending')], [], []), ZCFG),
                         [Action('revoke', 100, 'CI pending on the approved commit')])
        self.assertEqual([a.kind for a in plan(Snapshot([self.ready_pr(checks='fail')], [], []),
                                               ZCFG)], ['ci_failed'])

    def test_a_level_0_pr_in_review_still_waits_for_its_ci(self):
        # Reviews start with the CI running (#271), but level 0 has no review to start early.
        for checks, wait in (('pending', ''), ('none', 'waiting for CI')):
            with self.subTest(checks=checks):
                p = dataclasses.replace(pr(labels=('agent:review', 'reviews:0'), checks=checks),
                                        ci_wait=wait)
                self.assertEqual(plan(Snapshot([p], [], []), ZCFG), [])
                # Without level 0 enabled the label means nothing: the review starts.
                self.assertEqual([a.kind for a in plan(Snapshot([p], [], []), CFG)], ['review'])

    def test_through_ticks(self):
        gh = SnapshotGH([])
        gh.labels[100] = set(ZERO_LABELS)
        gh.zero_events, gh.runs = list(GRANT), list(SUCCESS)
        ws = FakeWS()
        ws.diff = changes('docs/a.txt')
        runner = FakeRunner(raw_review())
        d = Dispatcher(ZCFG, gh, ws, runner, log=lambda m: None)
        self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(gh.labels[100], {'reviews:0', 'agent:ready-to-merge'})
        self.assertEqual(d.snapshot().prs[0].zero_head, SHA1)
        # [merge] is off: the merge is the operator's (#248), and the label the dispatcher's.
        self.assertEqual(d.tick(), [Action('operator_merge', 100, 'merge mode off')])
        self.assertEqual(d.tick(), [])
        # The operator removes the label: readiness goes, and the PR gets a level-1 review.
        gh.labels[100].discard('reviews:0')
        gh.zero_events.append({'event': 'unlabeled', 'by': 'brushtyler', 'at': AT})
        self.assertEqual(d.tick(), [Action('revoke', 100, 'the level 0 promotion no longer holds')])
        self.assertIn('Livello 0 sospeso', published(gh)[-1])
        self.assertEqual(gh.labels[100], {'agent:review'})
        self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(records(gh, 'policy')[-1]['receipt']['level'], 1)
        self.assertIsNone(d.snapshot().prs[0].zero_head)

    def promoted(self, cfg=ZCFG):
        """A dispatcher, its GitHub and its runner after a level-0 promotion of PR #100."""
        gh = SnapshotGH([])
        gh.labels[100] = set(ZERO_LABELS)
        gh.zero_events, gh.runs = list(GRANT), list(SUCCESS)
        ws = FakeWS()
        ws.diff = changes('docs/a.txt')
        runner = FakeRunner(raw_review())
        d = Dispatcher(ZCFG, gh, ws, runner, log=lambda m: None)
        self.assertEqual(d.tick(), [Action('review', 100)])
        # With [merge] off the merge is the operator's (#248): the dispatcher's own label.
        self.assertEqual(d.tick(), [Action('operator_merge', 100, 'merge mode off')])
        self.assertEqual(d.tick(), [])
        self.assertEqual(gh.labels[100],
                         {'reviews:0', 'agent:ready-to-merge', 'agent:needs-operator'})
        return Dispatcher(cfg, gh, ws, runner, log=lambda m: None), gh, runner

    def assert_revoked(self, d, gh, why):
        self.assertEqual(d.tick(), [Action('revoke', 100, 'the level 0 promotion no longer holds')])
        self.assertIn('Livello 0 sospeso', published(gh)[-1])
        self.assertIn(why, published(gh)[-1])
        self.assertEqual(gh.labels[100], {'reviews:0', 'agent:review'})

    def test_the_level_is_recomputed_on_every_tick(self):
        # The label removed and added again by someone untrusted, the labels unchanged.
        d, gh, runner = self.promoted()
        gh.zero_events += [{'event': 'unlabeled', 'by': 'stranger', 'at': AT},
                           {'event': 'labeled', 'by': 'stranger', 'at': AT}]
        self.assert_revoked(d, gh, 'assegna il livello 1')
        self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(records(gh, 'review')[-1]['receipt']['level'], 1)
        # The level-2 label added by a trusted user, then removed: level 2 prevails.
        d, gh, runner = self.promoted()
        gh.level_labelers = ['brushtyler']
        self.assert_revoked(d, gh, 'assegna il livello 2')
        # The operator's configuration no longer lists the file.
        narrower = dataclasses.replace(ZPOLICY, zero_allowlist=('tests/',))
        d, gh, runner = self.promoted(dataclasses.replace(ZCFG, review_policy=narrower))
        self.assert_revoked(d, gh, '`docs/a.txt`')

    def test_expected_checks_are_rechecked_strictly(self):
        for conclusion in ('neutral', 'skipped', None):
            with self.subTest(conclusion=conclusion):
                d, gh, runner = self.promoted()
                # A newer run of the expected check: the summary of CI may still pass.
                gh.runs.append(dict(SUCCESS[0], id=2, conclusion=conclusion,
                                    status='completed' if conclusion else 'in_progress'))
                state = conclusion or 'pending'
                self.assert_revoked(d, gh, f'`unittest` (app {APP}): `{state}`')
                if conclusion:
                    # Still level 0, the check is no success: a human decides.
                    self.assertEqual(d.tick(), [Action('review', 100)])
                    self.assertEqual(gh.labels[100], {'reviews:0', 'agent:needs-operator'})
                    self.assertEqual(runner.calls, [])

    def test_a_later_request_for_changes_revokes(self):
        d, gh, runner = self.promoted()
        gh.native_reviews = [{'login': 'stranger', 'state': 'CHANGES_REQUESTED'}]
        self.assertEqual(d.tick(), [])
        gh.native_reviews.append({'login': 'brushtyler', 'state': 'CHANGES_REQUESTED'})
        self.assert_revoked(d, gh, '`REQUEST_CHANGES`')
        self.assertEqual(d.tick(), [Action('review', 100)])
        self.assertEqual(gh.labels[100], {'reviews:0', 'agent:needs-operator'})
        self.assertEqual(runner.calls, [])


if __name__ == '__main__':
    unittest.main()
