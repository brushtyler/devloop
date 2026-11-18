"""devloop: the continuous mode of ``devloop run`` (#227), without a barrier at the end of a tick.

The loop runs with simulated agents, whose turns last a given time on a controlled clock: the
clock advances only when the loop sleeps, and an agent's turn ends once the clock has passed its
duration."""

import dataclasses
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import activity, config, runloop
from devloop.config import INSTALL_ROOT
from devloop.dispatcher import Dispatcher, Workers
from devloop.github import GitHub, GitHubError
from devloop.pin import Repinned, SmokeError
from devloop.planning import Action, Snapshot, admit, plan
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
    ``events``, ``(time, description)`` that the clock makes true (``prs_at``,
    ``ready_at``)."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.Lock()
        self.prs = {}
        self.issues = {}  # number: time it becomes ready
        self.prs_at = {}  # PR number: time it shows up (opened, or moved to agent:changes)
        self.announced = set()

    def events(self):
        return sorted(set(self.issues.values()) | set(self.prs_at.values()))

    def snapshot(self):
        now = self.clock()
        with self.lock:
            prs = []
            for p in self.prs.values():
                if now < self.prs_at.get(p.number, 0):
                    continue
                p = dataclasses.replace(p, labels=set(p.labels), reviews=list(p.reviews))
                prs.append(p)
            issues = [ready(n) for n, at in sorted(self.issues.items()) if now >= at]
        return Snapshot(prs, issues, [])


class Sim(Dispatcher):
    """A dispatcher on a ``World``, whose actions are simulated agent turns of ``durations``
    seconds (by ``(kind, number)``, else by kind). Each action records its span on the clock and checks that no other
    action on the same numbers, and no other merge, runs with it."""

    def __init__(self, cfg, world, clock, durations, stop_after=None, stop_file=None,
                 pinner=None):
        self.logs = []
        super().__init__(cfg, FakeGH(), FakeWS(), FakeRunner(), log=self.logs.append,
                         pinner=pinner)
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
                if held & numbers or kind == other == 'merge':
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
        def done(n):
            self.world.prs.pop(n)
            if self.pinner is not None:
                self.pinner.request(n, 'head')
        self._turn_of('merge', target, done)

    def _do_operator_merge(self, target):
        """The merge is the operator's (#248): the world records the notice, and the PR is
        parked from then on."""
        with self.world.lock:
            p = self.world.prs[target.number]
            p.operator_merges = [p.head_sha]
            p.labels = p.labels | {'agent:needs-operator'}
            p.operator_owned = True

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

    def run_loop(self, d, ends=0):
        """Run the loop of ``d`` until it ends with ``ends``: its exit code, or the exception
        it raises."""
        d.workers = Workers(d.cfg.max_parallel_actions)
        loop = runloop.Loop(d.tick, self.watch, self.stop, log=self.logs.append,
                            idle_timeout=900, clock=self.clock, sleep=self.clock.sleep,
                            workers=d.workers)
        done = []

        def run():
            try:
                done.append(loop.run())
            except Exception as exc:  # Repinned, for the restart of #301
                done.append(exc)
        thread = threading.Thread(target=run)
        with mock.patch('signal.signal'):  # signal handlers only in the main thread
            thread.start()
            thread.join(timeout=60)
        self.assertFalse(thread.is_alive(), 'the loop did not end')
        if isinstance(ends, type):
            self.assertIsInstance(done[0], ends, done)
        else:
            self.assertEqual(done, [ends])
        self.assertEqual(d.violations, [])
        return d


class NoBarrierTests(ContinuousLoopCase):
    def test_a_review_starts_while_a_long_implementation_runs(self):
        # #165 is implemented for 46 minutes; #223 enters agent:review after 5 minutes.
        self.world.issues[165] = 0
        self.world.prs[223] = pr(223, issue=220, labels=['agent:review'])
        self.world.prs_at[223] = 5 * MINUTE
        d = Sim(PCFG, self.world, self.clock,
                {'implement': 46 * MINUTE, 'review': 10 * MINUTE},
                stop_after=('implement', 165), stop_file=self.stop)
        self.run_loop(d)
        implement, review = d.spans[('implement', 165)], d.spans[('review', 223)]
        # The review starts at the news of the PR, not when the implementation ends.
        self.assertGreaterEqual(review[0], 5 * MINUTE)
        self.assertLess(review[0], 8 * MINUTE)
        self.assertLess(review[1], implement[1])
        self.assertTrue(overlap(implement, review))
        # [merge] is off: the merge is the operator's (#248).
        self.assertEqual(self.world.prs[223].labels, {READY, 'agent:needs-operator'})
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
    def test_a_ready_pr_is_merged_while_a_long_implementation_runs(self):
        # #100 is fixed, reviewed and merged; #101 reviewed; #9 implemented for 46 minutes;
        # #10 becomes ready after 20. The merge of #100 does not wait for #9 (#264).
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
        # The merge starts as soon as the review of #100 ends, while #9 still runs.
        merge, implement = spans[('merge', 100)], spans[('implement', 9)]
        self.assertLess(merge[0] - spans[('review', 100)][1], 2 * MINUTE)
        self.assertTrue(overlap(merge, implement))
        self.assertLess(merge[1], implement[1])
        self.assertNotIn(100, self.world.prs)
        # Different numbers ran together, and #10 did not wait for #9.
        self.assertTrue(overlap(spans[('fix', 100)], implement))
        self.assertTrue(overlap(spans[('review', 101)], implement))
        self.assertLess(spans[('implement', 10)][0], implement[1])

    def test_two_merges_never_overlap_and_hold_back_no_other_action(self):
        # #100 and #101 are both ready; each merge lasts 30 minutes. #10 becomes ready after 5,
        # and that news starts its implementation while the first merge runs.
        for n, issue in ((100, 7), (101, 8)):
            p = pr(n, issue=issue, labels=[READY], reviews=[norm(raw_review())])
            p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', 'strict'
            self.world.prs[n] = p
        self.world.issues[10] = 5 * MINUTE
        cfg = dataclasses.replace(STRICT, max_parallel_actions=3, max_open_prs=5)
        d = Sim(cfg, self.world, self.clock, {'implement': 10 * MINUTE, 'merge': 30 * MINUTE},
                stop_after=('merge', 101), stop_file=self.stop)
        self.run_loop(d)  # no violation: no merge ran with another one
        first, second = d.spans[('merge', 100)], d.spans[('merge', 101)]
        implement = d.spans[('implement', 10)]
        self.assertEqual(first[0], 0)
        self.assertFalse(overlap(first, second))
        self.assertGreaterEqual(second[0], first[1])
        self.assertIn('news on GitHub: next tick', self.logs)
        self.assertTrue(any(line.endswith('running: merge #100') for line in d.logs), d.logs)
        self.assertTrue(overlap(first, implement))
        self.assertLess(implement[0], 6 * MINUTE)
        self.assertEqual(self.world.prs, {})



class SimPinner:
    """The pin after a merge (#174) on the simulated clock: each pin records its moment and
    the actions running then, and points ``current`` at a new copy; ``refuse`` makes the smoke
    test of #260 refuse every copy."""

    def __init__(self, clock, tmp, refuse=False):
        self.clock, self.tmp, self.refuse = clock, Path(tmp), refuse
        self.requests, self.pins, self.cancelled = [], [], 0
        self.dispatcher = None
        self.pointer = INSTALL_ROOT

    def pending(self):
        return self.requests[-1] if self.requests else None

    def request(self, number, head):
        self.requests.append({'pr': number, 'head': head})

    def cancel(self):
        self.cancelled += 1
        self.requests.clear()

    def pin(self, ref):
        self.pins.append((self.clock(), ref,
                          [a for a, _ in self.dispatcher.workers.running()]))
        if self.refuse:
            raise SmokeError('smoke test of 0123456789ab failed at tick --dry-run: '
                             'ImportError: broken', '0123456789ab' * 3 + '0123', 'tick --dry-run')
        self.requests.clear()
        self.pointer = self.tmp / f'pinned-{len(self.pins):012x}'
        return self.pointer

    def current(self):
        return self.pointer


class PinRestartTests(ContinuousLoopCase):
    """#301: the pin after a merge runs at once, the planning goes on with the loaded code, and
    the restart from the new copy happens when nothing runs, or drains after the limit."""

    def setUp(self):
        super().setUp()
        # #100 is ready and its merge (1 minute) requests the pin while #9 is implemented.
        p = pr(100, issue=7, labels=[READY], reviews=[norm(raw_review())])
        p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', 'strict'
        self.world.prs[100] = p
        self.world.issues[9] = 0
        # #101 enters agent:review after 5 minutes; #102 goes to agent:changes after 10.
        self.world.prs[101] = pr(101, issue=8, labels=['agent:review'])
        self.world.prs_at[101] = 5 * MINUTE
        self.world.prs[102] = pr(102, issue=11, labels=['agent:changes'])
        self.world.prs_at[102] = 10 * MINUTE

    def sim(self, durations, refuse=False, wait=30, **kw):
        pinner = SimPinner(self.clock, self.tmp, refuse=refuse)
        cfg = dataclasses.replace(STRICT, max_parallel_actions=3, max_open_prs=5,
                                  pin=dataclasses.replace(STRICT.pin, auto=True,
                                                          restart_max_wait_minutes=wait))
        d = Sim(cfg, self.world, self.clock, {'merge': MINUTE, 'review': 5 * MINUTE,
                                              'fix': 5 * MINUTE, **durations},
                pinner=pinner, **kw)
        d.clock = self.clock
        d.mergeable = (100,)
        pinner.dispatcher = d
        restarts = []
        real = d._restart_if_pending

        def restart_if_pending():
            try:
                real()
            except Repinned:
                restarts.append((self.clock(), d.workers.running()))
                raise
        d._restart_if_pending = restart_if_pending
        return d, pinner, restarts

    def test_the_pin_runs_at_once_and_review_and_fix_start_until_nothing_runs(self):
        d, pinner, restarts = self.sim({'implement': 45 * MINUTE})
        self.run_loop(d, ends=Repinned)
        merge, implement = d.spans[('merge', 100)], d.spans[('implement', 9)]
        review, fix = d.spans[('review', 101)], d.spans[('fix', 102)]
        # The pin ran right after the merge, while #9 was still being implemented ...
        self.assertEqual(len(pinner.pins), 1)
        moment, ref, running = pinner.pins[0]
        self.assertEqual((ref, running), ('origin/main', [Action('implement', 9)]))
        self.assertGreaterEqual(moment, merge[1])
        self.assertLess(moment, merge[1] + 2 * MINUTE)
        # ... the review and the fix started after it, with the restart pending ...
        self.assertGreater(review[0], moment)
        self.assertLess(review[0], 8 * MINUTE)
        self.assertGreater(fix[0], moment)
        self.assertLess(fix[0], 13 * MINUTE)
        self.assertLess(fix[1], implement[1])
        self.assertTrue(any(line.startswith('restart to 000000000001 pending since ')
                            and line.endswith('(1 running): no new implement until the restart')
                            for line in d.logs), d.logs)
        # ... and the restart came at the first tick with nothing running, from the new copy.
        (when, still), = restarts
        self.assertEqual(still, [])
        self.assertGreaterEqual(when, implement[1])
        self.assertLess(when, implement[1] + 2 * MINUTE)
        self.assertIn('nothing runs: restart from pinned-000000000001', d.logs)
        self.assertIsNone(d._restart)

    def test_with_a_restart_pending_no_implementation_starts_but_review_and_fix_do(self):
        # #342: #12 is ready 2 minutes in, after the pin of the merge of #100, while #9 is
        # implemented for 12 minutes: it is not implemented while the restart waits, the review
        # of #101 (5 minutes in) and the fix of #102 (10 minutes in) start, and the restart
        # follows the end of the last running action, the fix, within the limit.
        self.world.issues[12] = 2 * MINUTE
        d, pinner, restarts = self.sim({'implement': 12 * MINUTE})
        self.run_loop(d, ends=Repinned)
        (moment, _, _), = pinner.pins
        self.assertLess(moment, 2 * MINUTE)
        self.assertNotIn(('implement', 12), d.spans)
        self.assertEqual(set(d.spans), {('merge', 100), ('implement', 9), ('review', 101),
                                        ('fix', 102)})
        review, fix = d.spans[('review', 101)], d.spans[('fix', 102)]
        implement = d.spans[('implement', 9)]
        self.assertGreater(review[0], moment)
        self.assertGreater(fix[0], moment)
        self.assertGreater(fix[1], implement[1])  # the fix is the last action to end
        (when, still), = restarts
        self.assertEqual(still, [])
        self.assertGreaterEqual(when, fix[1])
        self.assertLess(when, fix[1] + 2 * MINUTE)
        self.assertLess(when, moment + 30 * MINUTE)
        self.assertTrue(any(line.endswith(': no new implement until the restart')
                            for line in d.logs), d.logs)
        self.assertFalse(any('over [pin] restart_max_wait_minutes' in line for line in d.logs),
                         d.logs)
        self.assertIn('nothing runs: restart from pinned-000000000001', d.logs)
        self.assertIsNone(d._restart)
        self.assertEqual(d.violations, [])

    def test_over_the_limit_no_action_starts_until_the_restart(self):
        # The limit is 3 minutes: the review of #101 (in review after 5) and the fix of #102
        # (after 10) do not start while #9 is implemented for 45 minutes, nor does #12.
        self.world.issues[12] = 2 * MINUTE
        d, pinner, restarts = self.sim({'implement': 45 * MINUTE}, wait=3)
        self.run_loop(d, ends=Repinned)
        self.assertEqual(set(d.spans), {('merge', 100), ('implement', 9)})
        (when, still), = restarts
        self.assertEqual(still, [])
        self.assertGreaterEqual(when, d.spans[('implement', 9)][1])
        self.assertTrue(any('over [pin] restart_max_wait_minutes (3), no action starts until '
                            'the restart' in line for line in d.logs), d.logs)
        self.assertEqual(len(pinner.pins), 1)

    def test_a_copy_refused_by_the_smoke_test_leaves_everything_as_before(self):
        # #260: the request is dropped, current stays, no restart waits, and the planning goes
        # on with the running copy: the review and the fix start, and so does the implementation
        # of #12 (#342), the loop ends at the STOP.
        self.world.issues[12] = 2 * MINUTE
        d, pinner, restarts = self.sim({'implement': 45 * MINUTE}, refuse=True,
                                       stop_after=('implement', 9), stop_file=self.stop)
        self.run_loop(d)
        self.assertEqual(len(pinner.pins), 1)
        self.assertEqual(pinner.cancelled, 1)
        self.assertIsNone(pinner.pending())
        self.assertEqual(pinner.current(), INSTALL_ROOT)
        self.assertIsNone(d._restart)
        self.assertEqual(restarts, [])
        self.assertIn(('review', 101), d.spans)
        self.assertIn(('fix', 102), d.spans)
        self.assertIn(('implement', 12), d.spans)
        self.assertTrue(any(line.startswith('auto-pin of origin/main refused, the pinned copy '
                                            'is unchanged and the request is dropped')
                            for line in d.logs), d.logs)
        self.assertFalse(any(line.startswith('restart to ') for line in d.logs), d.logs)


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


class EndDuringTickTests(unittest.TestCase):
    def test_the_last_action_ending_during_the_snapshot_plans_again_at_once(self):
        # max_open_prs=1: the running implementation of #9 holds the only PR slot. It ends,
        # without a PR, while the second tick reads GitHub: that plan still reserves its slot
        # and starts nothing, and the loop must plan again at once rather than wait 900 s.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        stop = tmp / 'STOP'
        cfg = dataclasses.replace(PCFG, max_open_prs=1)
        d = Dispatcher(cfg, FakeGH(), FakeWS(), FakeRunner(), log=lambda line: None)
        d.workers = Workers(3)
        now = [0.0]
        release, ran, reads, ticks, watches = threading.Event(), [], [], [], []

        def implement(number):
            ran.append(number)
            if number == 9:
                release.wait(timeout=10)
        d._do_implement = implement

        def snapshot():
            reads.append(now[0])
            if len(reads) == 2:  # #9 ends during this read
                release.set()
                deadline = time.monotonic() + 10
                while d.workers.running() and time.monotonic() < deadline:
                    time.sleep(0.01)
            return Snapshot([], [ready(n) for n in (9, 10) if n not in ran], [])
        d.snapshot = snapshot

        def tick():
            ticks.append(now[0])
            started = d.tick()
            if len(ticks) == 3:
                stop.touch()
            return started

        def watch(timeout, sleep, check):
            watches.append(now[0])
            check()
            if len(watches) == 1:
                return 0  # news: the second tick
            now[0] += timeout
            return NOTHING_NEW

        def sleep(seconds):
            now[0] += seconds
            time.sleep(0.001)
        logs = []
        loop = runloop.Loop(tick, watch, stop, log=logs.append, idle_timeout=900,
                            clock=lambda: now[0], sleep=sleep, workers=d.workers)
        with mock.patch('signal.signal'):
            self.assertEqual(loop.run(), 0)
        self.assertEqual(ran, [9, 10])
        self.assertEqual(ticks, [0, 0, 0])
        self.assertEqual(watches, [0])
        self.assertIn('an action ended during the tick: next tick', logs)


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
        actions = [Action('fix', 100), Action('pin', 0), Action('fix', 102)]
        self.assertEqual(admit(actions, {}, [], 3), [Action('pin', 0)])
        running = [(Action('implement', 9), frozenset({9}))]
        self.assertEqual(admit(actions, {}, running, 2), [])
        running = [(Action('merge', 101), frozenset({101, 8}))]
        self.assertEqual(admit(actions, {}, running, 2), [])

    def test_nothing_starts_while_a_global_action_runs(self):
        running = [(Action('pin', 0), frozenset({0}))]
        actions = [Action('fix', 101), Action('merge', 102), Action('implement', 9)]
        self.assertEqual(admit(actions, {101: 8, 102: 10}, running, 2), [])

    def test_a_ready_pr_holds_back_no_other_action(self):
        # #264: a merge is not global. With a long implementation running, it starts with the
        # review, readiness check and fix planned with it.
        running = [(Action('implement', 9), frozenset({9}))]
        actions = [Action('merge', 100), Action('review', 101), Action('preflight', 11),
                   Action('fix', 102)]
        issue_of = {100: 7, 101: 8, 102: 10}
        self.assertEqual(admit(actions, issue_of, running, 4), actions)
        # A merge that waits for another one holds back nothing either.
        running.append((Action('merge', 103), frozenset({103, 12})))
        self.assertEqual(admit(actions, issue_of, running, 3), actions[1:])

    def test_one_merge_at_a_time(self):
        actions = [Action('merge', 100), Action('merge', 101), Action('fix', 102)]
        self.assertEqual(admit(actions, {}, [], 3), [Action('merge', 100), Action('fix', 102)])
        running = [(Action('merge', 99), frozenset({99}))]
        self.assertEqual(admit(actions, {}, running, 3), [Action('fix', 102)])
        # The merge left waiting still holds its number: a later action on it does not start.
        self.assertEqual(admit([Action('merge', 100), Action('hold', 100)], {}, running, 3), [])


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

    def test_merges_waiting_for_the_lane_take_no_slot_of_the_plan(self):
        # #264: with a limit of 3, three ready PRs and a review, the merges that cannot start
        # do not fill the plan and leave the review waiting behind them.
        def ready_pr(number, issue):
            p = pr(number, issue=issue, labels=[READY], reviews=[norm(raw_review())])
            p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', 'strict'
            return p
        snap = Snapshot(prs=[ready_pr(100, 7), ready_pr(101, 8), ready_pr(102, 10),
                             pr(103, issue=11, labels=['agent:review'])],
                        ready_issues=[], working_issues=[])
        cfg = dataclasses.replace(STRICT, max_parallel_actions=4, max_open_prs=5,
                                  max_actions_per_tick=3)
        issue_of = {p.number: p.issue for p in snap.prs}
        # A tick keeps every merge, each a phase of its own, up to the limit.
        self.assertEqual(self.kinds(plan(snap, cfg)),
                         [('merge', 100), ('merge', 101), ('merge', 102)])
        merges = [('merge', 100), ('merge', 101), ('merge', 102)]
        # A merge runs: the review starts in a free slot, no merge does.
        running = [(Action('merge', 99), frozenset({99, 12}))]
        actions = plan(snap, cfg, busy={99, 12}, merging=True)
        self.assertEqual(self.kinds(actions), merges + [('review', 103)])
        self.assertEqual(self.kinds(admit(actions, issue_of, running, 3)), [('review', 103)])
        # No merge runs: the first merge starts with the review.
        actions = plan(snap, cfg, merging=False)
        self.assertEqual(self.kinds(actions), merges + [('review', 103)])
        self.assertEqual(self.kinds(admit(actions, issue_of, [], 4)),
                         [('merge', 100), ('review', 103)])

    def test_merges_after_the_spent_limit_start_none(self):
        # #264: with a limit of 1 spent by a review, the first merge is dropped and does not
        # take the lane, so the second one does not start in its place past the limit.
        def ready_pr(number, issue):
            p = pr(number, issue=issue, labels=[READY], reviews=[norm(raw_review())])
            p.mergeable, p.merge_status, p.merge_mode = 'MERGEABLE', 'CLEAN', 'strict'
            return p
        snap = Snapshot(prs=[pr(99, issue=6, labels=['agent:review']), ready_pr(100, 7),
                             ready_pr(101, 8)],
                        ready_issues=[], working_issues=[])
        cfg = dataclasses.replace(STRICT, max_parallel_actions=4, max_open_prs=5,
                                  max_actions_per_tick=1)
        issue_of = {p.number: p.issue for p in snap.prs}
        actions = plan(snap, cfg, merging=False)
        self.assertEqual(self.kinds(actions), [('review', 99)])
        self.assertEqual(self.kinds(admit(actions, issue_of, [], 3)), [('review', 99)])

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

    def resuming(self, max_open_prs):
        """A dispatcher whose conclave on issue #7 restarts an implementation (#142) and runs
        until ``self.release``: the action, and its item in the snapshot."""
        d = self.make()
        self.addCleanup(self.release.set)
        d.cfg = dataclasses.replace(PCFG, max_open_prs=max_open_prs)
        item = {'number': 7, 'calls': 1, 'opens': True,
                'reason': 'conclave resume: `implement` with the unanimous resolution'}

        def conclave(number):
            self.ran.append(('conclave', number))
            self.release.wait(timeout=10)
        d._do_conclave = conclave
        return d, item

    def test_a_conclave_restarting_an_implementation_holds_its_pr_slot_while_it_runs(self):
        # #142: max_open_prs = 1. The restart takes the only slot; on the ticks that follow,
        # while it runs, no other issue starts, whether its record is already marked resumed
        # (the item is gone) or not yet (the item is still there).
        d, item = self.resuming(1)
        d.snapshot = lambda: Snapshot([], [], [], conclaves=[item])
        self.assertEqual(d.tick(), [Action('conclave', 7, item['reason'], True)])
        d.snapshot = lambda: Snapshot([], [ready(8)], [])
        self.assertEqual(d.tick(), [])
        d.snapshot = lambda: Snapshot([], [ready(8)], [], conclaves=[item])
        self.assertEqual(d.tick(), [])
        self.assertEqual(self.ran, [('conclave', 7)])

    def test_a_running_conclave_takes_no_second_pr_slot(self):
        # #142: max_open_prs = 2. While the restart runs, its item still in the snapshot takes
        # no slot besides the one it holds: the other slot starts issue #8.
        d, item = self.resuming(2)
        d.snapshot = lambda: Snapshot([], [], [], conclaves=[item])
        self.assertEqual(d.tick(), [Action('conclave', 7, item['reason'], True)])
        d.snapshot = lambda: Snapshot([], [ready(8)], [], conclaves=[item])
        self.assertEqual(d.tick(), [Action('implement', 8)])

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

    def test_with_a_restart_pending_a_checked_issue_leaves_its_slot_to_a_check(self):
        # #342: max_open_prs = 2 and #9 is implemented, so one PR slot is free. #10 passed its
        # readiness check and comes before #11, which has none: with a restart pending #10 is
        # not implemented and takes no slot, so the check of #11 starts on a free worker.
        d = self.make()
        self.addCleanup(self.release.set)
        d.snapshot = lambda: Snapshot([], [ready(9)], [])
        self.assertEqual(d.tick(), [Action('implement', 9)])
        d.cfg = dataclasses.replace(PCFG, max_open_prs=2, preflight=CFG.reviewer)
        d.clock = lambda: 1_000_000
        d._restart = ('0123456789ab', 1_000_000)
        d._do_preflight = lambda number: self.ran.append(('preflight', number))
        checked = {'hash': 'h', 'record': {'hash': 'h', 'ready': True, 'gaps': []},
                   'recheck': False, 'owned': True}
        unchecked = {'hash': 'h', 'record': None, 'recheck': False, 'owned': False}
        issues = [dict(ready(10), preflight=checked), dict(ready(11), preflight=unchecked)]
        d.snapshot = lambda: Snapshot([], issues, [])
        self.assertEqual(d.tick(), [Action('preflight', 11, 'no readiness check yet')])
        # Without the restart #10 takes the slot, as before #342.
        self.assertEqual([(a.kind, a.number) for a in plan(d.snapshot(), d.cfg, {9}, 1)],
                         [('implement', 10)])
        # Without the readiness check no issue starts either while the restart waits.
        self.assertEqual(plan(Snapshot([], [ready(10)], []), PCFG, implement=False), [])

    def test_a_pending_auto_pin_pins_at_once_and_the_tick_still_plans(self):
        # #301: the pin runs while #9 runs, the restart waits, the review of #101 starts but
        # not the implementation of #10 (#342); `status` shows it.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        pinner = mock.Mock()
        pinner.pending.return_value = None
        d = self.make(pinner=pinner)

        def review(target):
            self.ran.append(('review', target.number))
            self.release.wait(timeout=10)
        d._do_review = review
        d.activity = activity.Activity(tmp, pid=4242)
        d.clock = lambda: 1_000_000
        d.cfg = dataclasses.replace(PCFG, pin=dataclasses.replace(PCFG.pin, auto=True))
        d.snapshot = lambda: Snapshot([], [ready(9)], [])
        self.assertEqual(d.tick(), [Action('implement', 9)])
        pinner.pending.return_value = {'pr': 100}
        pinner.pin.return_value = tmp / 'pinned-0123456789ab'
        d.snapshot = lambda: Snapshot([pr(101, issue=8, labels=['agent:review'])],
                                      [ready(9), ready(10)], [])
        logs = []
        d.log.sink = logs.append
        self.assertEqual(d.tick(), [Action('review', 101)])
        pinner.pin.assert_called_once_with('origin/main')
        self.assertEqual(d._restart, ('0123456789ab', 1_000_000))
        line = 'restart to 0123456789ab pending since 1970-01-12T13:46:40Z'
        self.assertIn(f'{line} (1 running): no new implement until the restart', logs)
        # `devloop status` reads it while this dispatcher runs; nothing once it is gone.
        deadline = time.monotonic() + 10
        while self.ran != [9, ('review', 101)] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.ran, [9, ('review', 101)])
        self.assertEqual(len(json.loads((tmp / activity.FILE).read_text())['actions']), 2)
        self.assertEqual(activity.describe_restart(tmp, alive=lambda pid: pid == 4242,
                                                   held=lambda path: True),
                         f'{line} (2 running): no new implement until the restart')
        # A later tick while they run starts no implementation either.
        d.snapshot = lambda: Snapshot([], [ready(9), ready(10)], [])
        self.assertEqual(d.tick(), [])
        self.assertIsNone(activity.describe_restart(tmp, alive=lambda pid: False,
                                                    held=lambda path: True))
        self.release.set()
        deadline = time.monotonic() + 10
        while d.workers.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        # Nothing runs: the restart, from the copy current points at now.
        pinner.pending.return_value = None
        pinner.current.return_value = tmp / 'pinned-0123456789ab'
        with self.assertRaises(Repinned) as raised:
            d.tick()
        self.assertEqual(raised.exception.copy, tmp / 'pinned-0123456789ab')
        self.assertIsNone(d._restart)
        self.assertFalse((tmp / activity.RESTART).exists())

    def test_the_limit_passed_during_the_snapshot_starts_no_action(self):
        # #301: the limit (30 minutes) is not over when the tick begins, 1799 s after the pin,
        # but passes while the snapshot reads GitHub: #10 does not start.
        pinner = mock.Mock()
        pinner.pending.return_value = None
        d = self.make(pinner=pinner)
        now = [0]
        d.clock = lambda: now[0]
        d.cfg = dataclasses.replace(PCFG, pin=dataclasses.replace(PCFG.pin, auto=True,
                                                                  restart_max_wait_minutes=30))
        d.snapshot = lambda: Snapshot([], [ready(9)], [])
        self.assertEqual(d.tick(), [Action('implement', 9)])
        d._restart = ('0123456789ab', 0)
        now[0] = 1799

        def snapshot():
            now[0] = 1801  # the reads of GitHub end past the limit
            return Snapshot([], [ready(9), ready(10)], [])
        d.snapshot = snapshot
        logs = []
        d.log.sink = logs.append
        self.assertEqual(d.tick(), [])
        self.assertEqual([a.number for a, _ in d.workers.running()], [9])
        self.assertTrue(any('over [pin] restart_max_wait_minutes (30), no action starts until '
                            'the restart' in line for line in logs), logs)
        self.release.set()

    def test_a_restart_to_the_running_copy_is_dropped(self):
        # The operator pinned the running copy again by hand while the restart waited.
        pinner = mock.Mock()
        pinner.pending.return_value = None
        pinner.current.return_value = INSTALL_ROOT
        d = self.make(pinner=pinner)
        d.cfg = dataclasses.replace(PCFG, pin=dataclasses.replace(PCFG.pin, auto=True))
        d._restart = ('0123456789ab', 0)
        d.snapshot = lambda: Snapshot([], [ready(9)], [])
        self.assertEqual(d.tick(), [Action('implement', 9)])
        self.assertIsNone(d._restart)
        self.release.set()

    def test_a_pin_requested_during_the_snapshot_runs_at_the_next_tick(self):
        # #264: a merge running alongside may end and request the pin while the tick reads
        # GitHub; the tick starts its actions all the same, and the next one pins.
        requests = []
        pinner = mock.Mock()
        pinner.pending.side_effect = lambda: requests[-1] if requests else None
        pinner.pin.return_value = Path('/state/pinned-0123456789ab')
        d = self.make(pinner=pinner)
        d.cfg = dataclasses.replace(PCFG, pin=dataclasses.replace(PCFG.pin, auto=True))
        d.snapshot = lambda: Snapshot([], [ready(9)], [])
        self.assertEqual(d.tick(), [Action('implement', 9)])

        def snapshot():
            requests.append({'pr': 100})  # the merge of #100 ends now
            return Snapshot([], [ready(9), ready(10)], [])
        d.snapshot = snapshot
        self.assertEqual(d.tick(), [Action('implement', 10)])
        pinner.pin.assert_not_called()
        d.snapshot = lambda: Snapshot([], [], [])
        self.assertEqual(d.tick(), [])
        pinner.pin.assert_called_once_with('origin/main')
        self.assertEqual(d._restart[0], '0123456789ab')
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
        text = (REPO / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        start = text.index('### Continuous mode')
        section = text[start:text.index('\n### ', start + 1)]
        for needle in ('`devloop run`', '`devloop tick`', 'max_parallel_actions', 'barrier',
                       'global action', 'same number', 'STOP'):
            self.assertIn(needle, section)

    def test_the_devloop_document_describes_the_merge_lane(self):
        text = (REPO / 'docs' / 'devloop' / 'guide.md').read_text(encoding='utf-8')
        start = text.index('#### The merge lane')
        section = text[start:text.index('\n### ', start)]
        for needle in ('one merge\nat a time', 'holds back no\nother action', 'pin after a merge',
                       'is not a global action either', 'restart_max_wait_minutes',
                       'no action is exclusive with a merge', '`devloop tick`'):
            self.assertIn(needle, section)
        protocol = (REPO / 'docs' / 'devloop' / 'protocol.md').read_text(encoding='utf-8')
        self.assertIn('[merge lane](guide.md#the-merge-lane)', protocol)


if __name__ == '__main__':
    unittest.main()
