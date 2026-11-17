"""devloop: the continuous mode of ``devloop run`` (#227), without a barrier at the end of a tick.

The loop runs with simulated agents, whose turns last a given time on a controlled clock: the
clock advances only when the loop sleeps, and an agent's turn ends once the clock has passed its
duration."""

import dataclasses
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config, runloop
from devloop.dispatcher import Action, Dispatcher, Snapshot, Workers, admit, plan
from devloop.github import GitHub, GitHubError
from devloop.watch import NOTHING_NEW
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeRunner, FakeWS, norm, pr, raw_review, ready
from test_devloop_merge import READY, STRICT

PCFG = dataclasses.replace(CFG, max_parallel_actions=3, max_open_prs=5)
REPO = Path(__file__).resolve().parent.parent
MINUTE = 60


class SimClock:
    """A monotonic clock shared by the loop and the simulated agents: it advances only when
    the loop sleeps, and wakes the agents whose turn is over. It never advances while an
    action's thread is working (``busy``) rather than waiting for its turn to end, so the times
    it shows do not depend on how fast the threads run."""

    def __init__(self):
        self.now = 0.0
        self.active = 0
        self._waiting = {}  # token: the moment its agent's turn ends
        self._cond = threading.Condition()

    def __call__(self):
        with self._cond:
            return self.now

    def busy(self, delta):
        with self._cond:
            self.active += delta
            self._cond.notify_all()

    def _release_due(self):
        for token, moment in list(self._waiting.items()):
            if self.now >= moment:
                del self._waiting[token]
                self.active += 1

    def sleep(self, seconds):
        deadline = time.monotonic() + 10
        with self._cond:
            while self.active and time.monotonic() < deadline:
                self._cond.wait(timeout=0.05)
            self.now += seconds
            self._release_due()
            self._cond.notify_all()

    def wait_until(self, moment):
        with self._cond:
            token = object()
            self._waiting[token] = moment
            self.active -= 1
            self._release_due()
            self._cond.notify_all()
            while token in self._waiting:
                self._cond.wait(timeout=0.05)


class World:
    """What GitHub shows: open PRs and ready issues, changed by the simulated actions and by
    ``events``, ``(time, description)`` that the clock makes true (``ci_green_at``,
    ``ready_at``)."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.prs = {}
        self.issues = {}  # number: time it becomes ready
        self.ci_green_at = {}  # PR number: time its CI passes
        self.announced = set()

    def events(self):
        return sorted(set(self.ci_green_at.values()) | set(self.issues.values()))

    def snapshot(self):
        now = self.clock()
        with self.lock:
            prs = []
            for p in self.prs.values():
                p = dataclasses.replace(p, labels=set(p.labels), reviews=list(p.reviews))
                if p.number in self.ci_green_at:
                    p.checks = 'pass' if now >= self.ci_green_at[p.number] else 'pending'
                prs.append(p)
            issues = [ready(n) for n, at in sorted(self.issues.items()) if now >= at]
        return Snapshot(prs, issues, [])


class Sim(Dispatcher):
    """A dispatcher on a ``World``, whose actions are simulated agent turns of ``durations``
    seconds (by ``(kind, number)``, else by kind). Each action records its span on the clock and checks that no other
    action on the same numbers, and no global action, runs with it."""

    def __init__(self, cfg, world, clock, durations, stop_after=None, stop_file=None):
        self.logs = []
        super().__init__(cfg, FakeGH(), FakeWS(), FakeRunner(), log=self.logs.append)
        self.world, self.clock, self.durations = world, clock, durations
        self.spans = {}  # (kind, number): (start, end)
        self.violations = []
        self.stop_after, self.stop_file = stop_after, stop_file
        self._active = []  # (kind, numbers) running now
        self._lock = threading.Lock()

    def snapshot(self):
        return self.world.snapshot()

    def tick(self, dry_run=False):
        started = super().tick(dry_run)
        self.clock.busy(len(started))  # their threads work until their turn waits
        return started

    def _run_started(self, action, prs):
        try:
            super()._run_started(action, prs)
        finally:
            self.clock.busy(-1)

    def _turn_of(self, kind, target, then):
        number = getattr(target, 'number', target)
        numbers = {number, getattr(target, 'issue', number)}
        with self._lock:
            for other, held in self._active:
                if held & numbers or 'merge' in (kind, other):
                    self.violations.append(((kind, number), other))
            entry = (kind, frozenset(numbers))
            self._active.append(entry)
        start = self.clock()
        self.clock.wait_until(start + self.durations.get((kind, number),
                                                         self.durations.get(kind)))
        with self._lock:
            self._active.remove(entry)
            self.spans[(kind, number)] = (start, self.clock())
        with self.world.lock:
            then(number)
        if (kind, number) == self.stop_after:
            self.stop_file.touch()

    def _do_implement(self, target):
        self._turn_of('implement', target, lambda n: self.world.issues.pop(n))

    def _do_fix(self, target):
        def done(n):
            p = self.world.prs[n]
            p.labels = {'agent:review'}
        self._turn_of('fix', target, done)

    def _do_review(self, target):
        def done(n):
            p = self.world.prs[n]
            p.labels = {READY}
            p.reviews = [norm(raw_review())]
            if n in self.mergeable:
                p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', 'strict'
        self._turn_of('review', target, done)

    def _do_merge(self, target):
        self._turn_of('merge', target, lambda n: self.world.prs.pop(n))

    mergeable = ()


def overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


class ContinuousLoopCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.stop = self.tmp / 'STOP'
        self.clock = SimClock()
        self.world = World(self.clock)
        self.logs = []
        self.watches = []
        patch = mock.patch.object(runloop, 'POLL_SECONDS', 10)
        patch.start()
        self.addCleanup(patch.stop)

    def watch(self, timeout, sleep, check):
        """News when one of the world's events came true since the last news."""
        start = self.clock()
        self.watches.append(start)
        while True:
            check()
            due = [t for t in self.world.events()
                   if t <= self.clock() and t not in self.world.announced]
            if due:
                self.world.announced.update(due)
                return 0
            if self.clock() - start >= timeout:
                return NOTHING_NEW
            sleep(min(MINUTE, timeout - (self.clock() - start)))

    def run_loop(self, d):
        d.workers = Workers(d.cfg.max_parallel_actions)
        loop = runloop.Loop(d.tick, self.watch, self.stop, log=self.logs.append,
                            idle_timeout=900, clock=self.clock, sleep=self.clock.sleep,
                            workers=d.workers)
        done = []
        thread = threading.Thread(target=lambda: done.append(loop.run()))
        with mock.patch('signal.signal'):  # signal handlers only in the main thread
            thread.start()
            thread.join(timeout=60)
        self.assertFalse(thread.is_alive(), 'the loop did not end')
        self.assertEqual(done, [0])
        self.assertEqual(d.violations, [])
        return d


class NoBarrierTests(ContinuousLoopCase):
    def test_a_review_starts_while_a_long_implementation_runs(self):
        # #165 is implemented for 46 minutes; the CI of #223 passes after 5 minutes.
        self.world.issues[165] = 0
        self.world.prs[223] = pr(223, issue=220, labels=['agent:review'], checks='pending')
        self.world.ci_green_at[223] = 5 * MINUTE
        d = Sim(PCFG, self.world, self.clock,
                {'implement': 46 * MINUTE, 'review': 10 * MINUTE},
                stop_after=('implement', 165), stop_file=self.stop)
        self.run_loop(d)
        implement, review = d.spans[('implement', 165)], d.spans[('review', 223)]
        # The review starts at the news of the green CI, not when the implementation ends.
        self.assertGreaterEqual(review[0], 5 * MINUTE)
        self.assertLess(review[0], 8 * MINUTE)
        self.assertLess(review[1], implement[1])
        self.assertTrue(overlap(implement, review))
        self.assertEqual(self.world.prs[223].labels, {READY})
        self.assertIn('news on GitHub: next tick', self.logs)
        self.assertIn('an action ended: next tick', self.logs)
        self.assertIn('outcome of review #223: done', d.logs)
        self.assertIn('outcome of implement #165: done', d.logs)

    def test_a_slot_freed_by_an_action_is_planned_at_once(self):
        # Three slots, four PRs to fix: the fourth starts once the short fix of #101 and its
        # review end, long before the other fixes do.
        for n in (100, 101, 102, 103):
            self.world.prs[n] = pr(n, issue=n - 90, labels=['agent:changes'])
        d = Sim(PCFG, self.world, self.clock,
                {'fix': 30 * MINUTE, ('fix', 101): 5 * MINUTE, 'review': MINUTE},
                stop_after=('fix', 103), stop_file=self.stop)
        self.run_loop(d)
        started = {n: d.spans[('fix', n)][0] for n in (100, 101, 102, 103)}
        self.assertEqual(max(started[n] for n in (100, 101, 102)), 0)
        self.assertGreaterEqual(started[103], d.spans[('fix', 101)][1])
        self.assertLess(started[103], d.spans[('fix', 100)][1])


class ExclusionTests(ContinuousLoopCase):
    def test_actions_on_the_same_number_never_overlap_and_a_merge_runs_alone(self):
        # #100 is fixed, reviewed and merged; #101 reviewed; #9 implemented for 46 minutes;
        # #10 becomes ready while the merge of #100 waits for the implementation to end.
        self.world.prs[100] = pr(100, issue=7, labels=['agent:changes'])
        self.world.prs[101] = pr(101, issue=8, labels=['agent:review'])
        self.world.issues[9] = 0
        self.world.issues[10] = 20 * MINUTE
        cfg = dataclasses.replace(STRICT, max_parallel_actions=3, max_open_prs=5)
        d = Sim(cfg, self.world, self.clock,
                {'implement': 46 * MINUTE, 'fix': 10 * MINUTE, 'review': 5 * MINUTE,
                 'merge': MINUTE},
                stop_after=('implement', 10), stop_file=self.stop)
        d.mergeable = (100,)
        self.run_loop(d)
        spans = d.spans
        self.assertEqual(set(spans), {('fix', 100), ('review', 100), ('merge', 100),
                                      ('review', 101), ('implement', 9), ('implement', 10)})
        # The same number, one action after the other.
        self.assertLessEqual(spans[('fix', 100)][1], spans[('review', 100)][0])
        self.assertLessEqual(spans[('review', 100)][1], spans[('merge', 100)][0])
        # The merge overlaps no other action: it waited for #9, and #10 waited for it.
        merge = spans[('merge', 100)]
        for key, span in spans.items():
            if key != ('merge', 100):
                self.assertFalse(overlap(merge, span), key)
        self.assertGreaterEqual(merge[0], spans[('implement', 9)][1])
        self.assertGreaterEqual(spans[('implement', 10)][0], merge[1])
        # Different numbers ran together.
        self.assertTrue(overlap(spans[('fix', 100)], spans[('implement', 9)]))
        self.assertTrue(overlap(spans[('review', 101)], spans[('implement', 9)]))


class StopAndFailureTests(ContinuousLoopCase):
    def test_a_stop_waits_for_the_running_actions_and_starts_no_other(self):
        self.world.issues[165] = 0
        self.world.issues[166] = 10 * MINUTE  # ready during the wait: never started
        d = Sim(dataclasses.replace(PCFG, max_open_prs=5), self.world, self.clock,
                {'implement': 30 * MINUTE})
        stop_at = 5 * MINUTE
        watch = self.watch

        def stopping_watch(timeout, sleep, check):
            if self.clock() < stop_at:
                sleep(stop_at)
                self.stop.touch()
            return watch(timeout, sleep, check)
        self.watch = stopping_watch
        self.run_loop(d)
        self.assertEqual(set(d.spans), {('implement', 165)})
        self.assertIn('stop requested (the STOP file): no further tick', self.logs)
        self.assertIn('waiting for implement #165 to end', self.logs)
        self.assertGreaterEqual(d.spans[('implement', 165)][1], 30 * MINUTE)

    def test_failed_ticks_wait_for_the_running_actions_before_the_alert(self):
        self.world.issues[165] = 0
        d = Sim(PCFG, self.world, self.clock, {'implement': 60 * MINUTE})
        reads = []
        world_snapshot = self.world.snapshot

        def snapshot():
            reads.append(self.clock())
            if len(reads) > 1:
                raise GitHubError('HTTP 502')
            return world_snapshot()
        d.snapshot = snapshot
        d.workers = Workers(3)
        alerts = []
        loop = runloop.Loop(d.tick, self.watch, self.stop, log=self.logs.append,
                            alert=alerts.append, alert_issue=7, idle_timeout=900,
                            clock=self.clock, sleep=self.clock.sleep, workers=d.workers)
        with mock.patch('signal.signal'):
            code = loop.run()
        self.assertEqual(code, runloop.FAILED)
        self.assertEqual(len(alerts), 1)
        # The implementation started by the first tick ended before the loop stopped.
        self.assertEqual(set(d.spans), {('implement', 165)})
        self.assertIn('waiting for implement #165 to end', self.logs)


class AdmitTests(unittest.TestCase):
    def test_only_the_first_action_on_a_number_or_a_pr_and_its_issue_starts(self):
        actions = [Action('settle', 7), Action('review', 100), Action('fix', 101),
                   Action('sync', 101), Action('implement', 9)]
        self.assertEqual(admit(actions, {100: 7, 101: 8}, [], 3),
                         [Action('settle', 7), Action('fix', 101), Action('implement', 9)])

    def test_no_more_than_the_free_slots_start(self):
        actions = [Action('fix', 100), Action('fix', 101), Action('fix', 102)]
        self.assertEqual(admit(actions, {}, [], 2), actions[:2])
        self.assertEqual(admit(actions, {}, [], 0), [])

    def test_a_global_action_starts_alone_and_only_when_nothing_runs(self):
        actions = [Action('fix', 100), Action('merge', 101), Action('fix', 102)]
        self.assertEqual(admit(actions, {}, [], 3), [Action('merge', 101)])
        running = [(Action('implement', 9), frozenset({9}))]
        self.assertEqual(admit(actions, {}, running, 2), [])


class PlanAroundRunningTests(unittest.TestCase):
    def snap(self):
        return Snapshot(prs=[pr(100, issue=7, labels=['agent:changes']),
                             pr(101, issue=8, labels=['agent:review'])],
                        ready_issues=[ready(9), ready(10), ready(11)], working_issues=[])

    def kinds(self, actions):
        return [(a.kind, a.number) for a in actions]

    def test_without_running_actions_the_plan_is_the_one_of_a_tick(self):
        cfg = dataclasses.replace(PCFG, max_actions_per_tick=10)
        self.assertEqual(plan(self.snap(), cfg), plan(self.snap(), cfg, frozenset(), 0))
        self.assertEqual(self.kinds(plan(self.snap(), cfg)),
                         [('fix', 100), ('review', 101), ('implement', 9), ('implement', 10),
                          ('implement', 11)])

    def test_busy_numbers_are_left_out_and_take_no_slot(self):
        cfg = dataclasses.replace(PCFG, max_actions_per_tick=2)
        # #100 runs; #9 runs its readiness check and holds a PR slot.
        actions = plan(self.snap(), cfg, busy={100, 7, 9}, reserved=1)
        self.assertEqual(self.kinds(actions), [('review', 101), ('implement', 10)])
        # The PR of a running issue is busy too.
        self.assertEqual(self.kinds(plan(self.snap(), cfg, busy={8})),
                         [('fix', 100), ('implement', 9)])

    def test_reserved_slots_count_against_max_open_prs(self):
        cfg = dataclasses.replace(PCFG, max_open_prs=4, max_actions_per_tick=10)
        self.assertEqual(self.kinds(plan(self.snap(), cfg, busy={9}, reserved=1)),
                         [('fix', 100), ('review', 101), ('implement', 10)])
        self.assertEqual(self.kinds(plan(self.snap(), cfg, busy={9, 10}, reserved=2)),
                         [('fix', 100), ('review', 101)])


class ContinuousTickTests(unittest.TestCase):
    def make(self, ledger=None, pinner=None):
        gh = FakeGH()
        d = Dispatcher(PCFG, gh, FakeWS(), FakeRunner(), log=lambda line: None, ledger=ledger,
                       pinner=pinner)
        d.workers = Workers(3)
        self.addCleanup(d.workers.close)
        self.release = threading.Event()
        self.ran = []

        def implement(number):
            self.ran.append(number)
            self.release.wait(timeout=10)
        d._do_implement = implement
        return d

    def test_the_turns_of_running_actions_are_not_flagged_incomplete(self):
        ledger = mock.Mock()
        ledger.pending.return_value = [9, 10]
        d = self.make(ledger=ledger)
        d.snapshot = lambda: Snapshot([], [ready(9)], [])
        checked = []
        d.gh.issue_comments = lambda number: checked.append(number) or []
        self.assertEqual(d.tick(), [Action('implement', 9)])
        checked.clear()
        ledger.settle.reset_mock()
        d.snapshot = lambda: Snapshot([], [ready(9)], [9])
        self.assertEqual(d.tick(), [])
        self.assertEqual(checked, [10])
        ledger.settle.assert_called_once_with(10)
        self.release.set()

    def test_a_pending_auto_pin_starts_nothing_while_actions_run(self):
        pinner = mock.Mock()
        pinner.pending.return_value = None
        d = self.make(pinner=pinner)
        d.cfg = dataclasses.replace(PCFG, pin=dataclasses.replace(PCFG.pin, auto=True))
        d.snapshot = lambda: Snapshot([], [ready(9), ready(10)], [])
        self.assertEqual(d.tick(), [Action('implement', 9), Action('implement', 10)])
        pinner.pending.return_value = {'pr': 100}
        d.snapshot = mock.Mock()
        self.assertEqual(d.tick(), [])
        d.snapshot.assert_not_called()
        pinner.pin.assert_not_called()
        self.release.set()

    def test_a_manual_tick_keeps_the_barrier(self):
        d = Dispatcher(PCFG, FakeGH(), FakeWS(), FakeRunner(), log=lambda line: None)
        self.assertIsNone(d.workers)
        ended = []
        d._do_implement = lambda number: (time.sleep(0.05), ended.append(number))
        d.snapshot = lambda: Snapshot([], [ready(9), ready(10)], [])
        self.assertEqual(d.tick(), [Action('implement', 9), Action('implement', 10)])
        self.assertEqual(sorted(ended), [9, 10])  # both ended before the tick returned


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def mode(self, parallel):
        cfg = dataclasses.replace(config.load_config(), max_parallel_actions=parallel)
        dispatcher = SimpleNamespace(tick=mock.Mock(return_value=[]), gh=mock.Mock(),
                                     identities=None)
        seen = []

        class Loop:
            def __init__(self, tick, watch, stop, **kw):
                seen.append(kw['workers'])

            def run(self):
                return 0
        with mock.patch.object(runloop, 'Loop', Loop):
            cli.run_loop(cfg, self.tmp, dispatcher, 900)
        return seen[0], getattr(dispatcher, 'workers', None)

    def test_run_is_continuous_only_with_parallel_actions(self):
        workers, set_on_dispatcher = self.mode(3)
        self.assertIsInstance(workers, Workers)
        self.assertIs(set_on_dispatcher, workers)
        workers.close()
        self.assertEqual(self.mode(1), (None, None))

    def test_the_time_limit_of_a_wait_does_not_reach_the_actions_threads(self):
        gh = GitHub('o/r')
        gh.time_left = lambda: 0
        seen = []
        thread = threading.Thread(target=lambda: seen.append(gh.time_left))
        thread.start()
        thread.join()
        self.assertEqual(seen, [None])
        self.assertEqual(gh.time_left(), 0)
        self.assertIsNone(GitHub('o/r').time_left)


class DocumentationTests(unittest.TestCase):
    def test_the_devloop_document_describes_both_modes(self):
        text = (REPO / 'docs' / 'devloop.md').read_text(encoding='utf-8')
        start = text.index('### Continuous mode')
        section = text[start:text.index('\n### ', start + 1)]
        for needle in ('`devloop run`', '`devloop tick`', 'max_parallel_actions', 'barrier',
                       'global action', 'same number', 'STOP'):
            self.assertIn(needle, section)


if __name__ == '__main__':
    unittest.main()
