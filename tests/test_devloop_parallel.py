"""devloop: the actions of a tick in parallel on different issues and PRs (#176)."""

import dataclasses
import re
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import activity, agents, config, follow
from devloop.agents import AgentError
from devloop.dispatcher import Action, Dispatcher, Snapshot, TickLog, schedule
from devloop.ledger import Ledger
from devloop.workspace import Workspace
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeRunner, FakeWS, pr, raw_review
from test_devloop_fallback import unavailable
from test_devloop_quorum import CORE, FIRST, SECOND, Quorum

PCFG = dataclasses.replace(CFG, max_parallel_actions=3)
DELAY = 0.4  # how long a simulated agent turn sleeps


def overlap(a, b):
    """Whether the time spans ``(start, end)`` ``a`` and ``b`` overlap."""
    return a[0] < b[1] and b[0] < a[1]


class SleepingRunner:
    """Agents that sleep ``delay`` seconds per turn, then answer: from ``outputs[agent key]`` in
    order, or with an approval. Safe across threads."""

    def __init__(self, outputs=None, delay=DELAY):
        self.outputs = {key: list(queue) for key, queue in (outputs or {}).items()}
        self.delay = delay
        self.spans = []  # (agent key, model, label, workdir, start, end)
        self.calls = []
        self._lock = threading.Lock()

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        start = time.monotonic()
        time.sleep(self.delay)
        with self._lock:
            self.spans.append((agent.key, agent.model, label, str(workdir), start,
                               time.monotonic()))
            self.calls.append((agent.key, mode, schema_name, prompt))
            queue = self.outputs.get(agent.key)
            out = queue.pop(0) if queue else raw_review()
        if isinstance(out, Exception):
            raise out
        return out

    header = FakeRunner.header


class CopyWS(FakeWS):
    """FakeWS whose review copies are named after their reviewer when several run at once."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.copies = []

    def review_copy(self, path, reviewer=None):
        name = f'{Path(path).name}-review' + (f'-{reviewer}' if reviewer else '')
        self.copies.append(name)
        return name, Path('/wt') / name


class Timed(Dispatcher):
    """Actions that only sleep and record when they ran, in which thread; ``fail`` maps
    ``(kind, number)`` to the exception the action raises after sleeping."""

    def __init__(self, cfg, gh, *, fail=None, log=None, lines=0):
        self.logs = []
        super().__init__(cfg, gh, FakeWS(), FakeRunner(),
                         log=log if log is not None else self.logs.append)
        self.spans = {}
        self.threads = {}
        self.fail = dict(fail or {})
        self.lines = lines
        self._spans_lock = threading.Lock()

    def _step(self, kind, target):
        number = getattr(target, 'number', target)
        start = time.monotonic()
        for i in range(self.lines):
            self.log(f'  step {i} of {number}\n  detail of {number}')
        time.sleep(DELAY / 2)
        with self._spans_lock:
            self.spans[(kind, number)] = (start, time.monotonic())
            self.threads[(kind, number)] = threading.current_thread()
        if (kind, number) in self.fail:
            raise self.fail[(kind, number)]

    def _do_fix(self, target):
        self._step('fix', target)

    def _do_sync(self, target):
        self._step('sync', target)

    def _do_settle(self, target):
        self._step('settle', target)

    def _do_pin(self, target):
        self._step('pin', target)


def run_plan(d, actions, prs=()):
    d.snapshot = lambda: Snapshot(list(prs), [], [])
    with mock.patch('devloop.dispatcher.plan', return_value=list(actions)):
        started = time.monotonic()
        d.tick()
        return time.monotonic() - started


class ConfigTests(unittest.TestCase):
    def test_default_is_one_and_this_repository_runs_three(self):
        data = tomllib.loads(config.DEFAULT_CONFIG.read_text())
        self.assertEqual(data['max_parallel_actions'], 3)
        self.assertEqual(config.load_config().max_parallel_actions, 3)
        omitted = {k: v for k, v in data.items() if k != 'max_parallel_actions'}
        self.assertEqual(config.parse_config(omitted).max_parallel_actions, 1)
        for value in (0, -1, '3', True):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                config.parse_config(dict(data, max_parallel_actions=value))


class ScheduleTests(unittest.TestCase):
    def test_actions_on_the_same_number_share_a_lane_in_plan_order(self):
        actions = [Action('fix', 100), Action('review', 101), Action('sync', 100)]
        self.assertEqual(schedule(actions, {100: 7, 101: 8}),
                         [[[(0, actions[0]), (2, actions[2])], [(1, actions[1])]]])

    def test_a_pr_and_its_issue_share_a_lane(self):
        actions = [Action('settle', 7), Action('implement', 9), Action('review', 100)]
        self.assertEqual(schedule(actions, {100: 7}),
                         [[[(0, actions[0]), (2, actions[2])], [(1, actions[1])]]])

    def test_lanes_joined_by_a_later_action_keep_plan_order(self):
        actions = [Action('settle', 7), Action('fix', 101), Action('review', 100)]
        # #100's issue is 7: it joins the lane of #7; #101 stays on its own.
        self.assertEqual(schedule(actions, {100: 7, 101: 8}),
                         [[[(0, actions[0]), (2, actions[2])], [(1, actions[1])]]])

    def test_a_global_action_is_a_phase_of_its_own(self):
        actions = [Action('fix', 100), Action('pin', 174), Action('fix', 101),
                   Action('sync', 102)]
        self.assertEqual(schedule(actions, {}), [
            [[(0, actions[0])]], [[(1, actions[1])]],
            [[(2, actions[2])], [(3, actions[3])]]])


class ParallelTickTests(unittest.TestCase):
    def test_three_reviews_on_different_prs_run_together(self):
        gh, runner, logs = FakeGH(), SleepingRunner(), []
        prs = [pr(100, issue=7), pr(101, issue=8), pr(102, issue=9)]
        for p in prs:
            gh.labels[p.number] = set(p.labels)
        d = Dispatcher(PCFG, gh, FakeWS(), runner, log=logs.append)
        d.snapshot = lambda: Snapshot(prs, [], [])
        started = time.monotonic()
        self.assertEqual(d.tick(), [Action('review', 100), Action('review', 101),
                                    Action('review', 102)])
        elapsed = time.monotonic() - started
        spans = [span[4:] for span in runner.spans]
        self.assertEqual(sorted(span[3] for span in runner.spans),
                         ['/wt/pr-100-review', '/wt/pr-101-review', '/wt/pr-102-review'])
        # The three agents slept at the same time: the tick lasts as long as the longest.
        for a in spans:
            for b in spans:
                self.assertTrue(overlap(a, b))
        self.assertLess(elapsed, 2 * DELAY)
        for p in prs:
            self.assertEqual(gh.labels[p.number], {'agent:ready-to-merge'})
        self.assertEqual(sorted(line for line in logs if line.startswith('outcome')),
                         [f'outcome of review #{n}: done' for n in (100, 101, 102)])

    def test_the_parallel_tick_is_bounded_by_max_parallel_actions(self):
        d = Timed(dataclasses.replace(PCFG, max_parallel_actions=2), FakeGH())
        run_plan(d, [Action('fix', 100), Action('fix', 101), Action('fix', 102)])
        spans = list(d.spans.values())
        at_once = max(sum(1 for s in spans if s[0] <= t[0] < s[1]) for t in spans)
        self.assertEqual(at_once, 2)

    def test_actions_on_the_same_number_stay_in_sequence(self):
        d = Timed(PCFG, FakeGH())
        run_plan(d, [Action('fix', 100), Action('sync', 100), Action('fix', 101)])
        fix, sync, other = d.spans[('fix', 100)], d.spans[('sync', 100)], d.spans[('fix', 101)]
        self.assertLessEqual(fix[1], sync[0])
        self.assertTrue(overlap(fix, other))

    def test_a_pr_and_its_issue_stay_in_sequence(self):
        d = Timed(PCFG, FakeGH())
        run_plan(d, [Action('settle', 7), Action('fix', 100)], prs=[pr(100, issue=7)])
        self.assertLessEqual(d.spans[('settle', 7)][1], d.spans[('fix', 100)][0])

    def test_a_global_action_overlaps_no_other(self):
        d = Timed(PCFG, FakeGH())
        run_plan(d, [Action('fix', 100), Action('pin', 174), Action('fix', 101),
                     Action('sync', 102)])
        pin = d.spans[('pin', 174)]
        self.assertLessEqual(d.spans[('fix', 100)][1], pin[0])
        for key in (('fix', 101), ('sync', 102)):
            self.assertLessEqual(pin[1], d.spans[key][0])
        self.assertTrue(overlap(d.spans[('fix', 101)], d.spans[('sync', 102)]))

    def test_a_failing_worker_does_not_stop_the_others_and_is_reported(self):
        gh = FakeGH()
        prs = [pr(100, issue=7), pr(101, issue=8), pr(102, issue=9)]
        for p in prs:
            gh.labels[p.number] = set(p.labels)
        d = Timed(PCFG, gh, fail={('fix', 100): KeyError('boom'),
                                  ('sync', 102): AgentError('agent died')})
        run_plan(d, [Action('fix', 100), Action('fix', 101), Action('sync', 102),
                     Action('sync', 100)], prs=prs)
        # Every action ran, the one after the failure on the same PR too.
        self.assertEqual(set(d.spans), {('fix', 100), ('fix', 101), ('sync', 102),
                                        ('sync', 100)})
        self.assertTrue(overlap(d.spans[('fix', 100)], d.spans[('fix', 101)]))
        # The unexpected exception escalates its PR like a failure; the other one as before.
        self.assertIn('agent:needs-human', gh.labels[100])
        self.assertEqual(gh.labels[101], {'agent:review'})
        self.assertIn('agent:needs-human', gh.labels[102])
        bodies = {n: body for n, body in gh.comments}
        self.assertIn('errore inatteso del dispatcher (KeyError', bodies[100])
        self.assertIn('Il passo `sync` è fallito: agent died.', bodies[102])
        outcomes = [line for line in d.logs if line.startswith('outcome')]
        self.assertEqual(outcomes, [
            "outcome of fix #100: unexpected error: KeyError: 'boom'",
            'outcome of fix #101: done',
            'outcome of sync #102: failed: agent died',
            'outcome of sync #100: done'])
        self.assertIn("#100   unexpected error: KeyError: 'boom'", d.logs)

    def test_log_lines_of_parallel_actions_do_not_mix(self):
        written = []

        def slow_sink(message):
            # One character at a time, yielding to other threads: without the tick log's
            # lock the lines of parallel actions would interleave.
            for char in message + '\n':
                written.append(char)
                time.sleep(0)
        d = Timed(PCFG, FakeGH(), log=slow_sink, lines=25)
        run_plan(d, [Action('fix', 100), Action('fix', 101), Action('fix', 102)])
        lines = ''.join(written).splitlines()
        self.assertEqual(len([line for line in lines if line.startswith('#')]),
                         3 * (1 + 2 * 25))
        for line in lines:
            if line.startswith(('outcome of ', 'open PRs: ')):
                continue
            match = re.fullmatch(r'#(\d+) (fix #(\d+)|  step \d+ of (\d+)|  detail of (\d+))',
                                 line)
            self.assertIsNotNone(match, line)
            number = next(g for g in match.groups()[2:] if g)
            self.assertEqual(match.group(1), number, line)

    def test_with_the_default_of_one_actions_run_in_order_in_the_tick(self):
        d = Timed(CFG, FakeGH())
        run_plan(d, [Action('fix', 100), Action('fix', 101)])
        self.assertLessEqual(d.spans[('fix', 100)][1], d.spans[('fix', 101)][0])
        self.assertEqual({t for t in d.threads.values()}, {threading.current_thread()})
        self.assertEqual(d.logs[1:], ['fix #100', 'fix #101'])
        self.assertTrue(d.logs[0].startswith('open PRs: '))
        # An unexpected exception still stops the tick, as before #176.
        d = Timed(CFG, FakeGH(), fail={('fix', 100): KeyError('boom')})
        with self.assertRaises(KeyError):
            run_plan(d, [Action('fix', 100), Action('fix', 101)])
        self.assertNotIn(('fix', 101), d.spans)


class ParallelReviewersTests(Quorum):
    def make_parallel(self, outputs=None, cfg=PCFG):
        self.make(cfg=cfg)
        self.runner = self.d.runner = SleepingRunner(outputs)
        self.ws = self.d.ws = CopyWS()
        self.ws.diff = list(CORE)
        return self.d

    def turns(self, key):
        return [span for span in self.runner.spans if span[0] == key]

    def test_both_reviewers_of_a_level_2_round_run_together(self):
        self.make_parallel()
        started = time.monotonic()
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        elapsed = time.monotonic() - started
        self.assertEqual(sorted(self.reviewed()), sorted([FIRST, SECOND]))
        (first,), (second,) = self.turns(FIRST), self.turns(SECOND)
        self.assertTrue(overlap(first[4:], second[4:]))
        self.assertLess(elapsed, 2 * DELAY)
        # Each in a copy of its own, both verdicts recorded in the same tick, in receipt order.
        self.assertEqual(sorted(self.ws.copies),
                         sorted([f'pr-100-review-{FIRST}', f'pr-100-review-{SECOND}']))
        reviews = self.records('review')
        self.assertEqual([(r['reviewer'], r['round']) for r in reviews], [(FIRST, 1), (SECOND, 1)])
        for r in reviews:
            self.assertEqual(r['profile'], self.d._profile(r['reviewer'], 'review'))
        # Each judged the head on its own: the same prompt, without the other's verdict.
        self.assertEqual(self.runner.calls[0][3], self.runner.calls[1][3])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(self.planned(), [])

    def test_a_failed_reviewer_keeps_the_other_verdict_and_the_round_waits_for_it(self):
        self.make_parallel({SECOND: [AgentError(f'{SECOND} crashed')]})
        self.d.tick()
        (kept,) = self.records('review')
        self.assertEqual((kept['reviewer'], kept['round'], kept['verdict']), (FIRST, 1, 'approve'))
        # The failure escalates as before.
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        self.assertIn(f'Il passo `review` è fallito: {SECOND} crashed.', self.gh.comments[-1][1])
        # Once a human resumes it, the round runs only the missing reviewer.
        self.gh.labels[100] = {'agent:review'}
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        # The first two ran together: their order is the order they ended in.
        reviewed = self.reviewed()
        self.assertEqual(sorted(reviewed[:2]), sorted([FIRST, SECOND]))
        self.assertEqual(reviewed[2:], [SECOND])
        self.assertEqual([(r['reviewer'], r['round']) for r in self.records('review')],
                         [(FIRST, 1), (SECOND, 1)])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_fallback_skips_the_profile_the_other_reviewer_reserved(self):
        first = CFG.agent(FIRST)
        second = dataclasses.replace(first, key=SECOND, model='other-model', fallback_models=(
            config.FallbackModel(first.model, 'same', first.effort),
            config.FallbackModel('third-model', 'third', first.effort)))
        cfg = dataclasses.replace(PCFG, agents=dict(PCFG.agents, **{SECOND: second}))
        self.make_parallel({SECOND: [unavailable(SECOND)]}, cfg=cfg)
        self.d.tick()
        # The first fallback is the tuple the first reviewer runs with: skipped, never run.
        self.assertEqual([span[1] for span in self.turns(SECOND)], ['other-model', 'third-model'])
        self.assertTrue(any(f'model {first.model} skipped: not independent' in line
                            for line in self.logs))
        reviews = {r['reviewer']: r for r in self.records('review')}
        self.assertEqual(reviews[FIRST]['profile']['model'], first.model)
        self.assertEqual(reviews[SECOND]['profile']['model'], 'third-model')
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_no_compatible_fallback_leaves_the_reviewer_waiting(self):
        first = CFG.agent(FIRST)
        second = dataclasses.replace(first, key=SECOND, model='other-model', fallback_models=(
            config.FallbackModel(first.model, 'same', first.effort),))
        cfg = dataclasses.replace(PCFG, agents=dict(PCFG.agents, **{SECOND: second}))
        self.make_parallel({SECOND: [unavailable(SECOND)]}, cfg=cfg)
        self.d.tick()
        self.assertEqual([span[1] for span in self.turns(SECOND)], ['other-model'])
        (kept,) = self.records('review')
        self.assertEqual(kept['reviewer'], FIRST)
        self.assertEqual(self.gh.labels[100], {'agent:needs-human'})
        self.assertIn(f'- `{first.model}`: not independent', self.gh.comments[-1][1])

    def test_with_the_default_of_one_a_review_action_runs_one_reviewer(self):
        self.make_parallel(cfg=CFG)
        self.ws.review_copy = FakeWS.review_copy.__get__(self.ws)  # the one-argument form
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST])
        self.assertEqual(self.gh.labels[100], {'agent:review'})


class SharedStateTests(unittest.TestCase):
    def test_each_parallel_action_has_its_own_activity_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = activity.Activity(tmp, clock=lambda: 1_000_000)
            started, finish = threading.Barrier(3), threading.Event()

            def action(kind, number, agent):
                with current.action(kind, number):
                    current.turn(agent, f'/logs/{number}.prompt.md')
                    started.wait()
                    finish.wait()
            threads = [threading.Thread(target=action, args=args)
                       for args in (('fix', 100, 'claude'), ('review', 101, 'codex'))]
            for t in threads:
                t.start()
            started.wait()
            record = activity.read(tmp)
            self.assertEqual(sorted((r['kind'], r['number'], r['agent'])
                                    for r in record['actions']),
                             [('fix', 100, 'claude'), ('review', 101, 'codex')])
            lines = activity.describe(tmp, now=1_000_010, held=lambda path: True,
                                      alive=lambda pid: True).split('\n')
            self.assertEqual(len(lines), 2)
            self.assertTrue(any('running: review #101 with codex' in line for line in lines))
            finish.set()
            for t in threads:
                t.join()
            self.assertIsNone(activity.read(tmp))

    def test_the_report_of_a_turn_belongs_to_its_thread(self):
        runner = agents.AgentRunner('/nonexistent', 10)
        runner.last_report = ('main', None)
        seen = []
        thread = threading.Thread(target=lambda: (seen.append(runner.last_report),
                                                  setattr(runner, 'last_report', ('other', None))))
        thread.start()
        thread.join()
        self.assertEqual(seen, [None])
        self.assertEqual(runner.last_report, ('main', None))

    def test_the_ledger_trace_counts_turns_of_parallel_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(FakeGH(), state_dir=tmp)

            def count():
                for _ in range(50):
                    ledger._count(100, +1)
            threads = [threading.Thread(target=count) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(ledger._load(), {'100': 200})

    def test_parallel_test_runs_get_a_temporary_directory_of_their_own(self):
        seen = []

        def run(argv, **kw):
            tmp = kw['env']['TMPDIR']
            seen.append((tmp, Path(tmp).is_dir()))
            return SimpleNamespace(returncode=0, stdout='ok', stderr='')
        ws = Workspace('/repo', '.state', 'main', run=run, test_env={'HOME': '/h'},
                       private_tmp=True)
        self.assertEqual(ws.run_tests('/wt/a', 'make test', 10), (True, 'ok'))
        ws.run_tests('/wt/b', 'make test', 10)
        (first, existed), (second, _) = seen
        self.assertTrue(existed)
        self.assertNotEqual(first, second)
        self.assertFalse(Path(first).exists())  # removed after the run
        # Without parallel actions the environment is the one configured, as before #176.
        calls = []
        plain = Workspace('/repo', '.state', 'main', test_env={'HOME': '/h'},
                          run=lambda argv, **kw: (calls.append(kw.get('env')),
                                                  SimpleNamespace(returncode=0, stdout='',
                                                                  stderr=''))[1])
        plain.run_tests('/wt/a', 'make test', 10)
        self.assertEqual(calls, [{'HOME': '/h'}])

    def test_review_copies_of_parallel_reviewers_have_their_own_names(self):
        # A real root: review_copy creates the parent of the copy, which outside a temporary
        # directory needs permissions the CI runner does not have.
        with tempfile.TemporaryDirectory() as repo:
            ws = Workspace(repo, '.state', 'main',
                           run=lambda argv, **kw: SimpleNamespace(returncode=0,
                                                                  stdout='a' * 40, stderr=''))
            self.assertEqual(ws.review_copy('/wt/pr-5')[0], 'pr-5-review')
            self.assertEqual(ws.review_copy('/wt/pr-5', 'agy')[0], 'pr-5-review-agy')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('pr-5-review-agy', 'pr-5-review-codex', 'pr-50-review-agy'):
                (root / name).mkdir()
            self.assertEqual(follow.worktrees(root, 'pr-5'), [
                root / 'pr-5', root / 'pr-5-review', root / 'pr-5-review-agy',
                root / 'pr-5-review-codex'])

    def test_tick_log_prefixes_each_line_only_inside_an_action(self):
        lines = []
        log = TickLog(lines.append)
        log('plain\nmessage')
        with log.prefixed('#7'):
            log('a\nb')
        self.assertEqual(lines, ['plain\nmessage', '#7 a', '#7 b'])


if __name__ == '__main__':
    unittest.main()
