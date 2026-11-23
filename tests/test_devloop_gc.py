"""devloop: the cleanup of state_dir (#380): pinned copies, worktrees of closed work, old logs."""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config, pin
from devloop.config import ConfigError, GCPolicy
from devloop.dispatcher import Dispatcher
from devloop.github import GitHub, GitHubError
from devloop.housekeeping import Housekeeping
from devloop.ledger import LedgerError
from helpers import ssdlc_project
from test_devloop import CFG, FakeRunner, FakeWS
from devloop.workspace import Workspace
from test_devloop_pin import GitCase, git

DAY = 86400
NOW = 1_800_000_000


def gc_cfg(**kw):
    return dataclasses.replace(CFG, gc=dataclasses.replace(GCPolicy(), **kw))


class ConfigTests(unittest.TestCase):
    def test_defaults_clean_pins_and_worktrees_not_logs(self):
        gc = ssdlc_project.load().gc
        self.assertEqual((gc.pins, gc.keep_pins, gc.worktrees, gc.logs, gc.log_days,
                          gc.interval_minutes), (True, 5, True, False, 14, 60))

    def test_the_overlay_turns_the_retention_of_the_logs_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / 'config.local.toml'
            local.write_text('[gc]\nlogs = true\nlog_days = 30\nkeep_pins = 2\n')
            gc = ssdlc_project.load(local).gc
        self.assertEqual((gc.logs, gc.log_days, gc.keep_pins, gc.pins), (True, 30, 2, True))

    def test_invalid_values_stop_devloop(self):
        for table, error in (({'logs': 1}, 'gc.logs: expected true or false'),
                             ({'keep_pins': -1}, 'gc.keep_pins'),
                             ({'log_days': 0}, 'gc.log_days'),
                             ({'interval_minutes': True}, 'gc.interval_minutes'),
                             ({'bogus': 1}, 'gc: unknown keys')):
            with self.subTest(table=table), self.assertRaisesRegex(ConfigError, error):
                config.parse_gc(table)


class PinCleanupTests(GitCase):
    """After a successful pin, ``current`` and the last N copies stay (#380)."""

    def copies(self):
        return sorted(p.name for p in self.state.glob('pinned-*'))

    def commits(self, count):
        return [self.push(f'c{i}', 'devloop/extra.txt', f'{i}\n') for i in range(count)]

    def test_a_pin_keeps_current_and_the_last_n_copies(self):
        a, b, c, d = self.commits(4)
        for sha in (a, b, c):
            self.pin(sha, keep=2, in_use=lambda copies: set())
        self.assertEqual(len(self.copies()), 3)
        # A reuse (the rollback) is a successful pin too: its time moves on.
        self.pin(a[:12], keep=2, in_use=lambda copies: set())
        self.pin(d, keep=2, in_use=lambda copies: set())
        self.assertEqual(self.pointed().name, f'pinned-{d[:12]}')
        self.assertEqual(self.copies(), sorted(f'pinned-{s[:12]}' for s in (a, c, d)))
        self.assertIn(f'gc: removed the pinned copy pinned-{b[:12]}', self.logs)
        times = json.loads((self.state / pin.TIMES).read_text())
        self.assertEqual(sorted(times), self.copies())

    def test_without_keep_every_copy_stays(self):
        for sha in self.commits(3):
            self.pin(sha)
        self.assertEqual(len(self.copies()), 3)

    def test_a_copy_in_use_stays_and_a_later_cleanup_removes_it(self):
        a, b = self.commits(2)
        self.pin(a, keep=0)
        busy = f'pinned-{a[:12]}'
        self.pin(b, keep=0, in_use=lambda copies: {busy})
        self.assertIn(busy, self.copies())
        self.assertIn(f'gc: {busy} is still in use, kept for a later cleanup', self.logs)
        self.assertEqual(pin.prune_now(self.state, 0, log=self.logs.append,
                                       in_use=lambda copies: set()), [busy])
        self.assertEqual(self.copies(), [f'pinned-{b[:12]}'])

    def test_nothing_is_removed_when_the_processes_cannot_be_read(self):
        a, b = self.commits(2)
        self.pin(a, keep=0, in_use=lambda copies: set())
        self.pin(b, keep=0, in_use=lambda copies: None)
        self.assertEqual(len(self.copies()), 2)
        self.assertTrue(any('cannot be read' in line for line in self.logs))

    def test_copies_pinned_before_the_record_are_ordered_by_their_time(self):
        a, b, c = self.commits(3)
        for sha in (a, b, c):
            self.pin(sha)
        (self.state / pin.TIMES).unlink()
        os.utime(self.state / f'pinned-{a[:12]}', (NOW, NOW))
        os.utime(self.state / f'pinned-{b[:12]}', (NOW - DAY, NOW - DAY))
        self.assertEqual(pin.prune_now(self.state, 1, log=self.logs.append,
                                       in_use=lambda copies: set()), [f'pinned-{b[:12]}'])
        self.assertEqual(self.copies(), sorted(f'pinned-{s[:12]}' for s in (a, c)))

    def test_a_rejected_copy_is_never_touched(self):
        a, b = self.commits(2)
        (self.state / f'rejected-{a[:12]}').mkdir(parents=True)
        self.pin(a, keep=0, in_use=lambda copies: set())
        self.pin(b, keep=0, in_use=lambda copies: set())
        self.assertTrue((self.state / f'rejected-{a[:12]}').is_dir())


class RealWorktreeTests(GitCase):
    def test_the_git_worktree_of_a_closed_issue_is_removed(self):
        ws = Workspace(self.root, '.devloop', 'main')
        ws.base_copy('issue-66')
        ws.base_copy('issue-70')
        keeper = Housekeeping(self.state, ws, GCGH(issues={66: 'CLOSED'}), ledger=Ledger(),
                              log=self.logs.append, in_use=lambda copies: set())
        self.assertEqual(keeper.run(gc_cfg(pins=False))['worktrees'], ['issue-66'])
        self.assertEqual(ws.names(), ['issue-70'])
        listed = git(self.root, 'worktree', 'list', '--porcelain')
        self.assertNotIn('issue-66', listed)
        self.assertIn('issue-70', listed)


class InUseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.copies = [self.tmp / 'pinned-aaa', self.tmp / 'pinned-bbb', self.tmp / 'pinned-ccc']
        for copy in self.copies:
            copy.mkdir()
        self.proc = self.tmp / 'proc'

    def process(self, pid, environ=(), cmdline=(), cwd=None, uid=1000, readable=True):
        entry = self.proc / str(pid)
        entry.mkdir(parents=True)
        (entry / 'status').write_text(f'Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        if readable:
            (entry / 'environ').write_bytes(b'\0'.join(os.fsencode(v) for v in environ))
        else:  # as a process not dumpable: reading its environment fails
            (entry / 'environ').mkdir()
        (entry / 'cmdline').write_bytes(b'\0'.join(os.fsencode(v) for v in cmdline))
        if cwd:
            os.symlink(cwd, entry / 'cwd')

    def in_use(self):
        return pin.copies_in_use(self.copies, self.proc, uid=1000)

    def test_a_process_uses_a_copy_through_its_path_its_command_or_its_directory(self):
        self.process(1, environ=['HOME=/x', f'PYTHONPATH={self.copies[0]}'])
        self.process(2, cmdline=['python3', str(self.copies[1] / 'devloop' / '__main__.py')])
        self.process(3, cwd=self.copies[2] / '.')
        self.process(4, environ=['PYTHONPATH=/elsewhere'], cmdline=['sleep', '1'], cwd='/')
        (self.proc / 'self').mkdir()
        self.assertEqual(self.in_use(), {'pinned-aaa', 'pinned-bbb', 'pinned-ccc'})

    def test_other_users_and_processes_gone_do_not_count(self):
        self.process(1, environ=[f'PYTHONPATH={self.copies[0]}'], uid=0)
        self.process(2, uid=0, readable=False)
        (self.proc / '3').mkdir()  # gone: its entries are no longer there
        self.process(4, environ=[f'PYTHONPATH={self.copies[1]}'])
        self.assertEqual(self.in_use(), {'pinned-bbb'})

    def test_a_process_of_the_user_that_cannot_be_read_may_use_any_copy(self):
        self.process(1, environ=[f'PYTHONPATH={self.copies[0]}'])
        self.process(2, cmdline=['ssh-agent'], readable=False)
        self.assertIsNone(self.in_use())

    @unittest.skipUnless(sys.platform.startswith('linux') and Path('/proc/self/status').exists(),
                         'needs the /proc of Linux')
    def test_a_live_process_not_dumpable_keeps_the_copy_it_uses(self):
        """A process of this user that turned off ``PR_SET_DUMPABLE`` hides its environment:
        the copy on its ``PYTHONPATH`` cannot be told unused."""
        child = subprocess.Popen(
            [sys.executable, '-c', 'import ctypes, sys; ctypes.CDLL(None).prctl(4, 0, 0, 0, 0); '
                                   'print("ready", flush=True); sys.stdin.read()'],
            env=dict(os.environ, PYTHONPATH=str(self.copies[0])), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE)
        self.addCleanup(child.wait)
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.stdout.close)
        self.assertEqual(child.stdout.readline().strip(), b'ready')
        used = pin.copies_in_use(self.copies)
        # Unreadable (None), or read by a user who may read it (root): never ``unused``.
        self.assertTrue(used is None or 'pinned-aaa' in used, used)

    def test_without_proc_it_cannot_tell(self):
        self.assertIsNone(pin.copies_in_use(self.copies, self.tmp / 'missing'))


class GCWS:
    """The worktrees of the cleanup, by name."""

    def __init__(self, root, names):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        for name in names:
            (self.root / name).mkdir(parents=True)

    def names(self):
        return sorted(p.name for p in self.root.iterdir())

    def path(self, name):
        return self.root / name

    def remove(self, name):
        (self.root / name).rmdir()


class GCGH:
    """GitHub as the cleanup reads it: the state and the labels of each issue and PR, read one by
    one (``fail``: the numbers whose read fails, or True for every one)."""

    def __init__(self, issues=None, prs=None, labels=None, fail=False):
        self.issues, self.prs, self.labels, self.fail = issues or {}, prs or {}, labels or {}, fail
        self.reads = []

    def _read(self, kind, number, state):
        self.reads.append((kind, number))
        if self.fail is True or number in (self.fail or ()):
            raise GitHubError('HTTP 502')
        return {'number': number, 'state': state,
                'labels': [{'name': name} for name in self.labels.get(number, ())]}

    def issue(self, number):
        return self._read('issue', number, self.issues.get(number, 'OPEN'))

    def pr_state(self, number):
        return self._read('pr', number, self.prs.get(number, 'OPEN'))


class Ledger:
    def __init__(self, pending=(), error=None):
        self._pending, self.error = list(pending), error

    def pending(self):
        if self.error:
            raise self.error
        return list(self._pending)


class HousekeepingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = self.tmp / 'state'
        self.state.mkdir()
        self.logs = []

    def keeper(self, gh, names=(), pending=(), ledger=None, clock=lambda: NOW):
        self.ws = GCWS(self.state / 'worktrees', names)
        return Housekeeping(self.state, self.ws, gh, ledger=ledger or Ledger(pending),
                            log=self.logs.append, clock=clock, in_use=lambda copies: set())


class WorktreeTests(HousekeepingCase):
    def test_the_worktrees_of_a_closed_issue_go_one_in_use_stays(self):
        gh = GCGH(issues={66: 'CLOSED', 93: 'CLOSED', 70: 'OPEN'},
                  prs={101: 'MERGED', 102: 'CLOSED', 103: 'OPEN'})
        names = ['issue-66', 'issue-66-preflight', 'issue-93', 'issue-70', 'pr-101',
                 'pr-101-review', 'pr-102-review-codex', 'pr-103', 'scratch']
        keeper = self.keeper(gh, names, pending=[93])  # #93 still has a turn open
        removed = keeper.run(gc_cfg())['worktrees']
        self.assertEqual(sorted(removed), ['issue-66', 'issue-66-preflight', 'pr-101',
                                           'pr-101-review', 'pr-102-review-codex'])
        self.assertEqual(self.ws.names(), ['issue-70', 'issue-93', 'pr-103', 'scratch'])
        self.assertNotIn(('issue', 93), gh.reads)
        self.assertIn('gc: removed the worktree issue-66 (issue #66 closed)', self.logs)
        self.assertIn('gc: removed the worktree pr-101 (pr #101 merged)', self.logs)

    def test_a_state_that_cannot_be_read_keeps_the_worktree(self):
        keeper = self.keeper(GCGH(fail=True), ['issue-66'])
        self.assertEqual(keeper.run(gc_cfg())['worktrees'], [])
        self.assertEqual(self.ws.names(), ['issue-66'])
        self.assertTrue(any('cannot be read, its worktrees stay' in line for line in self.logs))

    def test_an_unreadable_ledger_keeps_every_worktree(self):
        keeper = self.keeper(GCGH(issues={66: 'CLOSED'}), ['issue-66'],
                             ledger=Ledger(error=LedgerError('not readable')))
        self.assertEqual(keeper.run(gc_cfg())['worktrees'], [])
        self.assertEqual(self.ws.names(), ['issue-66'])

    def test_off_removes_nothing(self):
        keeper = self.keeper(GCGH(issues={66: 'CLOSED'}), ['issue-66'])
        keeper.run(gc_cfg(worktrees=False))
        self.assertEqual(self.ws.names(), ['issue-66'])


class LogRetentionTests(HousekeepingCase):
    def write(self, name, age_days):
        logs = self.state / 'logs'
        logs.mkdir(exist_ok=True)
        path = logs / name
        path.write_text('x')
        os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
        return path

    def left(self):
        return sorted(p.name for p in (self.state / 'logs').iterdir())

    def test_the_evidence_of_an_open_escalation_stays(self):
        old = {
            'timeout-issue-7-implement.diff': 20,  # #7 in agent:needs-operator
            # The turn's log the escalation of #7 cites (``Dispatcher._log_hint``).
            '20261101T101010-issue-7-implement-1-claude.log': 20,
            'timeout-pr-9-fix-r2.diff.used-20261101T101010': 20,  # PR #9 in needs-supervisor
            'conclave-issue-8-123.diff': 20,  # #8 no longer escalated
            '20261101T101010-issue-12-implement-1-claude.log': 20,  # #12 closed, label left
            '20261101T101010-issue-5-implement-1-claude.prompt.md': 20,  # turn still open
            '20261101T101010-pr-11-review-codex.log': 20,
            '20261117T101010-pr-11-review-codex.log': 1,  # recent
            'timeout-unknown.diff': 20,  # an evidence without its issue or PR
        }
        for name, age in old.items():
            self.write(name, age)
        gh = GCGH(issues={12: 'CLOSED'},
                  labels={7: ['agent:needs-operator'], 9: ['agent:needs-supervisor'],
                          11: ['agent:review'], 12: ['agent:needs-operator']})
        keeper = self.keeper(gh, pending=[5])
        removed = keeper.run(gc_cfg(logs=True, worktrees=False))['log files']
        self.assertEqual(sorted(removed), ['20261101T101010-issue-12-implement-1-claude.log',
                                           '20261101T101010-pr-11-review-codex.log',
                                           'conclave-issue-8-123.diff'])
        self.assertEqual(self.left(), [
            '20261101T101010-issue-5-implement-1-claude.prompt.md',
            '20261101T101010-issue-7-implement-1-claude.log',
            '20261117T101010-pr-11-review-codex.log',
            'timeout-issue-7-implement.diff',
            'timeout-pr-9-fix-r2.diff.used-20261101T101010',
            'timeout-unknown.diff'])
        self.assertIn('gc: removed the log file conclave-issue-8-123.diff (older than 14 days)',
                      self.logs)
        # Each issue or PR is read once, fresh, and not the one with a turn open.
        self.assertEqual(sorted(gh.reads), [('issue', 7), ('issue', 8), ('issue', 12),
                                            ('pr', 9), ('pr', 11)])

    def test_an_item_that_cannot_be_read_keeps_its_files(self):
        self.write('timeout-issue-7-implement.diff', 20)
        self.write('20261101T101010-issue-7-implement-1-claude.log', 20)
        self.write('20261101T101010-pr-11-review-codex.log', 20)
        keeper = self.keeper(GCGH(fail={7}))
        keeper.run(gc_cfg(logs=True, worktrees=False))
        self.assertEqual(self.left(), ['20261101T101010-issue-7-implement-1-claude.log',
                                       'timeout-issue-7-implement.diff'])
        self.assertTrue(any('issue #7 cannot be read, its log files stay' in line
                            for line in self.logs))

    def test_an_escalation_beyond_the_limit_of_the_lists_stays(self):
        """The lists of GitHub stop at 200 items (``open_prs``, ``issues_with_label``): the
        cleanup reads each PR it would touch, so PR #250, the 201st open one, keeps its files."""
        self.write('timeout-pr-250-fix-r1.diff', 20)
        self.write('20261101T101010-pr-250-fix-r1-claude.log', 20)
        self.write('20261101T101010-pr-251-review-codex.log', 20)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv[1:3])
            if argv[1:3] == ['pr', 'view']:
                number = int(argv[3])
                labels = [{'name': 'agent:needs-supervisor'}] if number == 250 else []
                out = {'state': 'OPEN', 'labels': labels}
            else:  # a list: the first 200 open PRs, without #250
                out = [{'number': n, 'labels': []} for n in range(1, 201)]
            return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr='')

        self.keeper(GitHub('o/r', run=run)).run(gc_cfg(logs=True, worktrees=False))
        self.assertEqual(self.left(), ['20261101T101010-pr-250-fix-r1-claude.log',
                                       'timeout-pr-250-fix-r1.diff'])
        self.assertEqual(sorted(calls), [['pr', 'view'], ['pr', 'view']])

    def test_the_retention_is_off_by_default(self):
        self.write('20261101T101010-issue-7-implement-1-claude.log', 400)
        self.keeper(GCGH()).run(gc_cfg())
        self.assertEqual(self.left(), ['20261101T101010-issue-7-implement-1-claude.log'])


class TickTests(HousekeepingCase):
    def test_the_cleanup_waits_for_its_interval(self):
        now = [NOW]
        gh = GCGH(issues={66: 'CLOSED', 67: 'CLOSED'})
        keeper = self.keeper(gh, ['issue-66'], clock=lambda: now[0])
        cfg = gc_cfg(interval_minutes=60)
        self.assertEqual(keeper.tick(cfg)['worktrees'], ['issue-66'])
        (self.ws.root / 'issue-67').mkdir()
        now[0] += 30 * 60
        self.assertIsNone(keeper.tick(cfg))
        self.assertEqual(self.ws.names(), ['issue-67'])
        now[0] += 31 * 60
        self.assertEqual(keeper.tick(cfg)['worktrees'], ['issue-67'])
        self.assertIsNone(keeper.tick(gc_cfg(pins=False, worktrees=False, interval_minutes=0)))

    def dispatcher(self, running=None):
        calls = []
        keeper = SimpleNamespace(tick=lambda cfg: calls.append(cfg))
        d = Dispatcher(CFG, None, FakeWS(), FakeRunner(), log=self.logs.append,
                       housekeeping=keeper)
        d.labels_checked = True
        d.snapshot = mock.Mock(side_effect=AssertionError('plan'))
        if running is not None:
            d.workers = SimpleNamespace(clear=lambda: None, running=lambda: running)
        return d, calls

    def test_a_tick_cleans_up_before_planning_a_dry_run_does_not(self):
        d, calls = self.dispatcher()
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick(dry_run=True)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()
        self.assertEqual(calls, [CFG])

    def test_in_continuous_mode_only_when_nothing_runs(self):
        d, calls = self.dispatcher(running=[(SimpleNamespace(kind='implement', number=7),
                                             frozenset({7}))])
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()
        self.assertEqual(calls, [])
        d, calls = self.dispatcher(running=[])
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()
        self.assertEqual(calls, [CFG])

    def test_a_failed_cleanup_does_not_stop_the_tick(self):
        d, _ = self.dispatcher()
        d.housekeeping = SimpleNamespace(tick=mock.Mock(side_effect=OSError('disk')))
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()
        self.assertIn('gc: the cleanup failed: OSError: disk', self.logs)

    def test_the_command_runs_every_cleanup_under_the_lock(self):
        runs = []
        keeper = SimpleNamespace(run=lambda cfg, verbose=False: runs.append((cfg, verbose)))
        dispatcher = SimpleNamespace(housekeeping=keeper)
        with mock.patch.object(cli, 'build', return_value=(CFG, self.state, dispatcher)):
            cli.main(['gc'])
        self.assertEqual(runs, [(CFG, True)])
        self.assertTrue((self.state / 'dispatcher.lock').exists())


if __name__ == '__main__':
    unittest.main()
