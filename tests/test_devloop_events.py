"""devloop: the stream of the events written by ``devloop run`` (#683), its producer."""

import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from devloop import events
from devloop.watch import ERROR_LINE, NOTHING_NEW, Watcher

from test_devloop_watch import TRUST, FakeGitHub

LINE = re.compile(r'(\d+) (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)')
NOW = datetime(2026, 11, 26, 10, 30, 5, tzinfo=timezone.utc)


class StreamCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = self.tmp / 'events'

    def stream(self, **kwargs):
        return events.Stream(self.tmp, now=lambda: NOW, **kwargs)

    def lines(self, name='events.log'):
        return [LINE.fullmatch(line).groups()
                for line in (self.dir / name).read_text().splitlines()]


class StreamTests(StreamCase):
    def test_each_event_is_a_line_with_its_number_and_time(self):
        stream = self.stream()
        stream.append(['PR #5 CI pass @abc1234', 'issue #7 labels +agent:needs-operator'])
        stream.append(['issue #8 operator reply by brushtyler: /devloop approve'])
        self.assertEqual(self.lines(), [
            ('1', '2026-11-26T10:30:05Z', 'PR #5 CI pass @abc1234'),
            ('2', '2026-11-26T10:30:05Z', 'issue #7 labels +agent:needs-operator'),
            ('3', '2026-11-26T10:30:05Z',
             'issue #8 operator reply by brushtyler: /devloop approve')])
        # A line is always one line: a newline in a label or a title cannot split it.
        stream.append(['new issue #9 [a\nb] T\ritle'])
        self.assertEqual(self.lines()[-1][2], 'new issue #9 [a b] T itle')

    def test_the_numbers_go_on_after_a_restart_and_a_half_written_line_is_removed(self):
        self.stream().append(['one', 'two'])
        # A crash in the middle of a write leaves a line without its newline: no reader took
        # it, and it never becomes an event.
        with (self.dir / 'events.log').open('a') as fh:
            fh.write('3 2026-11-26T10:31:00Z thr')
        self.stream().append(['three'])
        self.assertEqual([line[::2] for line in self.lines()],
                         [('1', 'one'), ('2', 'two'), ('3', 'three')])
        # The identity of the stream stays: a reader's cursor still holds.
        identity = (self.dir / 'id').read_text()
        self.stream().append(['four'])
        self.assertEqual((self.dir / 'id').read_text(), identity)
        self.assertEqual(self.lines()[-1][:1], ('4',))

    def test_rotation_keeps_the_current_segment_and_one_previous(self):
        stream = self.stream(max_bytes=120)
        for n in range(1, 9):
            stream.append([f'issue #{n} labels +agent:ready'])
        previous, current = self.lines('events.log.1'), self.lines()
        self.assertEqual(len(list(self.dir.glob('events.log*'))), 2)
        for segment in (previous, current):
            self.assertLessEqual(sum(len(' '.join(line)) + 1 for line in segment), 120)
        # The numbers go on across the segments, without gaps: the oldest ones are gone.
        numbers = [int(line[0]) for line in previous + current]
        self.assertEqual(numbers, list(range(numbers[0], 9)))
        self.assertGreater(numbers[0], 1)
        # A new producer finds the last number in the current segment, or in the previous one
        # (a restart just after a rotation).
        os.replace(self.dir / 'events.log', self.dir / 'events.log.1')
        self.stream().append(['issue #9 labels +agent:ready'])
        self.assertEqual(self.lines()[0][0], '9')
        self.assertEqual(events.MAX_SEGMENT, 10 * 1024 * 1024)

    def test_the_sign_of_life_and_the_modes_of_the_files(self):
        stream = self.stream()
        stream.alive()
        self.assertEqual((self.dir / 'alive').read_text(), f'{int(NOW.timestamp())}\n')
        stream.append(['PR #5 CI pass @abc1234'])
        # 640 and 750: the mode of a new file bounds the mask of the default ACL of the roles.
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        for name in ('alive', 'id', 'events.log'):
            self.assertEqual(stat.S_IMODE((self.dir / name).stat().st_mode) & 0o137, 0, name)
        self.assertEqual(stat.S_IMODE(self.dir.stat().st_mode) & 0o027, 0)
        # Replaced atomically: no temporary file left.
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ['alive', 'events.log', 'id'])

    def test_each_line_is_written_with_a_single_write(self):
        stream = self.stream()
        stream.open()
        writes = []
        real = os.write

        def write(fd, data):
            writes.append(bytes(data))
            return real(fd, data)
        with mock.patch.object(events.os, 'write', write):
            stream.append(['PR #5 CI pass @abc1234', 'issue #7 labels +agent:ready'])
        self.assertEqual(writes, [b'1 2026-11-26T10:30:05Z PR #5 CI pass @abc1234\n',
                                  b'2 2026-11-26T10:30:05Z issue #7 labels +agent:ready\n'])


class FailingWatcher:
    """A watcher whose rounds follow ``results``: lines, or None for a failed read. As
    ``watch.Watcher``, a round whose lines cannot be delivered fails and gives them again."""

    state = None
    state_path = Path('/nonexistent/run-watch-state.json')

    def __init__(self, results):
        self.results = list(results)
        self.gh = type('Client', (), {'time_left': None})()

    def round(self, deliver=None):
        result = self.results[0]
        if result:
            try:
                deliver(result)
            except OSError:
                return None
        self.results.pop(0)
        return result


class ProducerTests(StreamCase):
    def producer(self, watcher, **kwargs):
        self.logs = []
        return events.Producer(watcher, self.stream(), log=self.logs.append, **kwargs)

    def test_the_events_of_the_watcher_go_to_the_stream_before_its_state(self):
        gh = FakeGitHub()
        gh.add_pr(5)
        watcher = Watcher(gh, TRUST, self.tmp / 'run-watch-state.json',
                          now=lambda: NOW, out=lambda line: None, err=lambda line: None)
        producer = self.producer(watcher)
        self.assertEqual(producer.step(), [])  # the baseline: no event, but a read
        self.assertFalse((self.dir / 'events.log').exists())
        self.assertTrue((self.dir / 'alive').exists())
        gh.set_labels(5, 'agent:needs-operator')
        gh.add_issue(7)
        saved = []

        def deliver_then_fail(*args, **kwargs):
            saved.append((self.dir / 'events.log').read_text())
            raise OSError('disk full')
        with mock.patch('devloop.watch.save_state', deliver_then_fail):
            self.assertIsNone(producer.step())
        # Written before the state: the failed save repeats the events, never loses them.
        self.assertIn('PR #5 labels +agent:needs-operator', saved[0])
        lines = producer.step()
        self.assertEqual(lines, ['PR #5 labels +agent:needs-operator -agent:review',
                                 'new issue #7 [] issue 7'])
        self.assertEqual([line[2] for line in self.lines()], lines + lines)
        self.assertIn('news: PR #5 labels +agent:needs-operator -agent:review', self.logs)

    def test_three_failed_reads_give_the_error_line_and_keep_the_sign_of_life(self):
        producer = self.producer(FailingWatcher([[], None, None, None, None, ['PR #5 CI pass']]))
        producer.step()
        alive = (self.dir / 'alive').read_text()
        for _ in range(4):
            self.assertIsNone(producer.step())
        self.assertEqual([line[2] for line in self.lines()], [ERROR_LINE])
        self.assertEqual((self.dir / 'alive').read_text(), alive)  # no read succeeded
        producer.step()
        self.assertEqual([line[2] for line in self.lines()], [ERROR_LINE, 'PR #5 CI pass'])

    def test_the_wait_returns_on_news_written_before_or_during_it(self):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
        producer = self.producer(FailingWatcher([['PR #5 CI pass']]), clock=lambda: clock[0])
        self.assertEqual(producer.wait(5, sleep, lambda: None), NOTHING_NEW)
        self.assertEqual(clock[0], 5)
        producer.step()  # news during a tick
        self.assertEqual(producer.wait(5, sleep, lambda: None), 0)
        self.assertEqual(clock[0], 5)  # at once
        self.assertEqual(producer.wait(2, sleep, lambda: None), NOTHING_NEW)

    def test_a_stream_that_cannot_be_written_does_not_stop_the_reads(self):
        (self.tmp / 'events').write_text('not a directory\n')
        producer = self.producer(FailingWatcher([['PR #5 CI pass'], []]))
        self.assertIsNone(producer.step())
        self.assertTrue(any('could not be written' in line for line in self.logs), self.logs)
        # Once the stream can be written, the same read gives its events again.
        (self.tmp / 'events').unlink()
        self.assertEqual(producer.step(), ['PR #5 CI pass'])
        self.assertEqual([line[2] for line in self.lines()], ['PR #5 CI pass'])

    def watcher(self, gh):
        return Watcher(gh, TRUST, self.tmp / 'run-watch-state.json',
                       now=lambda: NOW, out=lambda line: None, err=lambda line: None)

    def test_a_restart_goes_on_from_the_saved_state(self):
        # #689: what changed while the loop was stopped is an event, not a new baseline.
        gh = FakeGitHub()
        gh.add_pr(5)
        self.assertEqual(self.producer(self.watcher(gh)).step(), [])
        gh.set_labels(5, 'agent:needs-operator')
        producer = self.producer(self.watcher(gh))
        self.assertEqual(producer.step(), ['PR #5 labels +agent:needs-operator -agent:review'])
        self.assertEqual([line[2] for line in self.lines()],
                         ['PR #5 labels +agent:needs-operator -agent:review'])
        self.assertEqual(producer.step(), [])
        # A state that cannot be read is said, and a new baseline replaces it.
        (self.tmp / 'run-watch-state.json').write_text('{not json\n')
        producer = self.producer(self.watcher(gh))
        self.assertEqual(producer.step(), [])
        self.assertIn('events: the state of the watcher is unreadable, a new baseline '
                      'replaces it', self.logs)

    def test_events_not_written_are_not_confirmed_and_come_again(self):
        # #689: a write that fails saves no state of the watcher and renews no sign of life;
        # once the stream recovers, the next read writes the same events.
        gh = FakeGitHub()
        gh.add_pr(5)
        producer = self.producer(self.watcher(gh))
        producer.step()
        state = (self.tmp / 'run-watch-state.json').read_text()
        (self.dir / 'alive').unlink()
        gh.set_labels(5, 'agent:needs-operator')
        with mock.patch.object(events.Stream, 'append', side_effect=PermissionError(13, 'x')):
            for _ in range(4):  # a write failure is no failed read: no alarm either
                self.assertIsNone(producer.step())
        self.assertFalse((self.dir / 'alive').exists())
        self.assertFalse((self.dir / 'events.log').exists())
        self.assertEqual((self.tmp / 'run-watch-state.json').read_text(), state)
        self.assertTrue(any('1 events could not be written' in line for line in self.logs))
        self.assertEqual(producer.step(), ['PR #5 labels +agent:needs-operator -agent:review'])
        self.assertEqual([line[2] for line in self.lines()],
                         ['PR #5 labels +agent:needs-operator -agent:review'])
        self.assertTrue((self.dir / 'alive').exists())

    def test_an_alarm_that_cannot_be_written_is_tried_again(self):
        producer = self.producer(FailingWatcher([None] * 5 + [[]]))
        with mock.patch.object(events.Stream, 'append', side_effect=PermissionError(13, 'x')):
            for _ in range(3):
                producer.step()
        producer.step()
        self.assertEqual([line[2] for line in self.lines()], [ERROR_LINE])
        producer.step()
        self.assertEqual([line[2] for line in self.lines()], [ERROR_LINE])

    def test_after_a_stop_no_other_read_starts(self):
        reads = []
        stopped = threading.Event()

        class Client:
            time_left = None

            def __getattr__(client, name):
                def read(*args):
                    client.time_left()  # as github.GitHub does before each gh call
                    reads.append(name)
                    stopped.set()
                    return []
                return read
        watcher = Watcher(Client(), TRUST, self.tmp / 'run-watch-state.json',
                          out=lambda line: None, err=lambda line: None)
        producer = self.producer(watcher, interval=0.01)
        producer.start(lambda: 'SIGTERM' if stopped.is_set() else None)
        producer._thread.join(10)
        self.assertFalse(producer._thread.is_alive())
        self.assertEqual(reads, ['open_prs'])
        self.assertFalse((self.tmp / 'run-watch-state.json').exists())
        producer.close()

    def test_close_ends_the_thread(self):
        watcher = FailingWatcher([[]] * 1000)
        producer = self.producer(watcher, interval=0.01)
        producer.start()
        producer.close()
        self.assertFalse(producer._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
