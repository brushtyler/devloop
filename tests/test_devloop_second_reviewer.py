"""devloop: when the second reviewer of a level 2 round starts (#521).

``[review_policy] second_reviewer`` is ``parallel`` by default: both reviewers of the pair run
together, as before. ``sequential`` runs the first of the pair alone, and the second on the same
head only once the first approved; ``parallel_cancel`` runs them together and stops the second's
turn as soon as the first asks for changes, recording it as cancelled. A round its first reviewer
closes without approving waits for no one and lacks no verdict: the supervisor's ``/devloop
rereview`` (#491) does not apply to it.
"""

import dataclasses
import re
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from devloop import agents, config, markdown, policy
from devloop.config import ConfigError, MergePolicy
from devloop.ledger import Ledger
from devloop.localtests import Prepare
from devloop.supervision import PREPARE_FAILED
from devloop.planning import Action, closed_by_first, missing_verdicts, pending_reviewers
from devloop.workspace import Cancelled, run_in_group
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeIdentities
from test_devloop import CFG, finding, raw_review
from test_devloop_ledger import ledger_records
from test_devloop_parallel import PCFG, CopyWS, SleepingRunner
from test_devloop_quorum import CORE, FIRST, SECOND, Identities, Quorum
from test_devloop_review_records import RegisterGH
from test_devloop_supervision import SUPER, header
from test_devloop_timeout_log import AGENT, _wait_gone

PARTS = 'Tocca la policy di fiducia: punto 4, `devloop/dispatcher.py` (condizioni di merge).'
REASON = 'le quote di agy erano esaurite; ora ha un modello disponibile'


def with_mode(cfg, mode):
    return dataclasses.replace(cfg, review_policy=dataclasses.replace(cfg.review_policy,
                                                                      second_reviewer=mode))


def changes():
    return raw_review('request_changes', [finding()])


def trust():
    """A ``needs_operator`` for the trust policy without findings of its own (#434)."""
    return raw_review('needs_operator', human=PARTS, reason='trust_policy')


def decision():
    return raw_review('needs_operator', human='serve una decisione', reason='decision')


class ConfigTests(unittest.TestCase):
    def parse(self, **table):
        data = ssdlc_project.data()
        data['review_policy'] = dict(data['review_policy'], **table)
        return config.parse_config(data)

    def test_parallel_by_default_and_out_of_the_version(self):
        self.assertEqual(ssdlc_project.load().review_policy.second_reviewer, 'parallel')
        default = self.parse()
        self.assertEqual(default.review_policy.second_reviewer, 'parallel')
        explicit = self.parse(second_reviewer='parallel')
        # The default keeps the version of before: no approval is voided.
        self.assertEqual(explicit.review_policy.version(), default.review_policy.version())
        self.assertIn('second_reviewer', config.OVERLAY_KEYS['review_policy'])

    def test_the_other_modes_enter_the_version(self):
        versions = {self.parse(second_reviewer=mode).review_policy.version()
                    for mode in policy.SECOND_REVIEWER_MODES}
        self.assertEqual(len(versions), 3)

    def test_an_unknown_mode_stops_devloop_at_startup(self):
        for value in ('serial', 'Sequential', '', True, 2):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as ctx:
                    self.parse(second_reviewer=value)
                text = str(ctx.exception)
                self.assertIn('review_policy.second_reviewer: unknown mode', text)
                self.assertIn('"parallel", "sequential", "parallel_cancel"', text)

    def test_the_receipt_records_the_mode_of_a_round_with_two_reviewers(self):
        def receipt(mode, reviewers):
            rp = dataclasses.replace(CFG.review_policy, second_reviewer=mode)
            return policy.evaluate(rp, pr=100, base='b' * 40, head='a' * 40, changes=[],
                                   labelers=[], trusted_users=(), previous_level=0,
                                   select=lambda level: reviewers)
        self.assertNotIn('second_reviewer', receipt('parallel', [FIRST, SECOND]))
        self.assertNotIn('second_reviewer', receipt('sequential', [FIRST]))
        self.assertEqual(receipt('sequential', [FIRST, SECOND])['second_reviewer'], 'sequential')
        self.assertIn('`sequential`', policy.describe(receipt('sequential', [FIRST, SECOND])))


class ParallelTests(Quorum):
    """Criterion 1: with ``parallel`` nothing changes."""

    def test_a_request_for_changes_of_the_first_still_waits_for_the_second(self):
        self.make(changes(), raw_review(), cfg=with_mode(CFG, 'parallel'))
        self.d.tick()
        (first,) = self.records('review')
        self.assertNotIn('second_reviewer', first['receipt'])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:changes'})

    def test_both_run_together_with_parallel_actions(self):
        self.make(cfg=with_mode(PCFG, 'parallel'))
        self.runner = self.d.runner = SleepingRunner({FIRST: [changes()]}, delay=0.1)
        self.ws = self.d.ws = CopyWS()
        self.ws.diff = list(CORE)
        self.d.tick()
        self.assertEqual(sorted(k for k, *_ in self.runner.calls), sorted([FIRST, SECOND]))
        self.assertEqual(sorted(r['reviewer'] for r in self.records('review')),
                         sorted([FIRST, SECOND]))
        self.assertEqual(self.gh.labels[100], {'agent:changes'})


class SequentialTests(Quorum):
    """Criterion 2: the second starts only after an approval of the first."""

    def setUp(self):
        self.cfg = with_mode(PCFG, 'sequential')  # parallel actions: the mode alone holds it

    def test_a_request_for_changes_closes_the_round_without_the_second(self):
        self.make(changes(), cfg=self.cfg)
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.assertEqual(self.reviewed(), [FIRST])
        (first,) = self.records('review')
        self.assertEqual(first['receipt']['second_reviewer'], 'sequential')
        # Closed with its verdict alone: the findings go to the fix.
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertEqual([a.kind for a in self.planned()], ['fix'])
        self.assertTrue(any('verdict of ' + FIRST in line and 'waits' not in line
                            for line in self.logs))

    def test_an_approval_starts_the_second_on_the_same_head(self):
        self.make(raw_review(), raw_review(), cfg=self.cfg)
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST])
        self.assertEqual(self.gh.labels[100], {'agent:review'})
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        reviews = self.records('review')
        self.assertEqual([(r['reviewer'], r['round'], r['head_sha']) for r in reviews],
                         [(FIRST, 1, self.gh.head), (SECOND, 1, self.gh.head)])
        # The quorum and the merge conditions do not change: both approvals promote.
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_trust_policy_needs_operator_starts_the_second(self):
        self.make(trust(), raw_review(), cfg=self.cfg)
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST])
        self.assertEqual(self.planned(), [Action('review', 100, f'the round waits for {SECOND}')])
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST, SECOND])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_decision_of_the_operator_closes_the_round(self):
        self.make(decision(), cfg=self.cfg)
        self.d.tick()
        self.assertEqual(self.reviewed(), [FIRST])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})


class CancellingRunner(SleepingRunner):
    """``SleepingRunner`` whose turns of ``slow`` last until their ``cancellable`` event is set
    (then ``TurnCancelled``), or ``patience`` seconds (then the next output)."""

    def __init__(self, outputs=None, slow=SECOND, patience=5.0):
        super().__init__(outputs, delay=0.05)
        self.slow, self.patience = slow, patience
        self.local = threading.local()
        self.cancelled = []

    @contextmanager
    def cancellable(self, event):
        self.local.cancel = event
        try:
            yield
        finally:
            self.local.cancel = None

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        cancel = getattr(self.local, 'cancel', None)
        if agent.key == self.slow and cancel is not None and cancel.wait(self.patience):
            with self._lock:
                self.cancelled.append(agent.key)
            raise agents.TurnCancelled(f'{agent.key} cancelled')
        return super().run(agent, mode, prompt, schema_name, workdir, label)


class ParallelCancelTests(Quorum):
    """Criterion 3: a request for changes of the first stops the second, which is cancelled."""

    def make_cancel(self, outputs, patience=5.0):
        cfg = with_mode(PCFG, 'parallel_cancel')
        self.make(cfg=cfg)
        self.gh = self.d.gh = RegisterGH()
        self.gh.labels[100] = {'agent:review'}
        self.d.identities = Identities(self.gh)
        self.gh.meta = {'issue': 7, 'implementer': 'claude', 'reviewer': cfg.reviewer,
                        'profile': self.d._profile('claude', 'write')}
        self.runner = self.d.runner = CancellingRunner(outputs, patience=patience)
        self.ws = self.d.ws = CopyWS()
        self.ws.diff = list(CORE)
        self.d.ledger = Ledger(self.gh)
        return self.d

    def turns(self):
        return {r['agent']: r for r in ledger_records(self.gh.pr_comments(100), 'turn')}

    def test_a_request_for_changes_cancels_the_second(self):
        self.make_cancel({FIRST: [changes()]})
        started = time.monotonic()
        self.assertEqual(self.d.tick(), [Action('review', 100)])
        self.assertLess(time.monotonic() - started, 3)  # not the second's patience
        self.assertEqual(self.runner.cancelled, [SECOND])
        # Only the first's verdict is recorded: the round is closed, the findings go to the fix.
        (first,) = self.records('review')
        self.assertEqual((first['reviewer'], first['receipt']['second_reviewer']),
                         (FIRST, 'parallel_cancel'))
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        # The second's turn is recorded as cancelled: not a failure, nothing escalates.
        turns = self.turns()
        self.assertEqual((turns[FIRST]['state'], turns[FIRST]['outcome']), ('closed', 'success'))
        self.assertEqual((turns[SECOND]['state'], turns[SECOND]['outcome']),
                         ('closed', 'cancelled'))
        self.assertFalse([r for r in self.records('notice') if r.get('event') == 'escalation'])
        self.assertTrue(any(f'review by {SECOND} cancelled' in line for line in self.logs))
        self.assertFalse(any(f'review by {SECOND} failed' in line for line in self.logs))
        self.assertEqual([a.kind for a in self.planned()], ['fix'])

    def test_an_approval_of_the_first_cancels_nothing(self):
        self.make_cancel({FIRST: [raw_review()]}, patience=0.3)
        self.d.tick()
        self.assertEqual(self.runner.cancelled, [])
        self.assertEqual(sorted(r['reviewer'] for r in self.records('review')),
                         sorted([FIRST, SECOND]))
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_a_failure_of_the_second_in_a_closed_round_does_not_escalate(self):
        self.make_cancel({FIRST: [changes()], SECOND: [agents.AgentError(f'{SECOND} crashed')]})
        self.runner.slow = None  # the second fails before the first ends
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        self.assertFalse([r for r in self.records('notice') if r.get('event') == 'escalation'])

    def test_a_failed_step_that_prepares_the_second_in_a_closed_round_escalates(self):
        # #696: the round is closed by the first, but the failed [prepare] cmd of the second's
        # copy is still the critical escalation to the operator, with its output.
        self.make_cancel({FIRST: [changes()]})
        self.d.cfg = dataclasses.replace(self.d.cfg, prepare=Prepare(
            command='npm ci --ignore-scripts', inputs=('package-lock.json',),
            outputs=('node_modules/',)))
        self.ws = self.d.ws = PrepareCopyWS(fail=SECOND)
        self.ws.diff = list(CORE)
        self.d.tick()
        self.assertEqual(sorted(self.ws.prepared_copies),
                         sorted([(f'pr-100-review-{FIRST}', True),
                                 (f'pr-100-review-{SECOND}', False)]))
        escalations = [r for r in self.records('notice') if r.get('event') == 'escalation']
        self.assertEqual([(r['class'], r['critical'], r['to']) for r in escalations],
                         [(PREPARE_FAILED, True, 'operator')])
        self.assertTrue(any('npm ERR! code E404' in c['body'] for c in self.d._pr_thread(100)))
        self.assertFalse(any('the failures above do not escalate' in line for line in self.logs))


class PrepareCopyWS(CopyWS):
    """A CopyWS with the step that prepares a worktree (#696): it fails in the copy of the
    reviewer ``fail``; ``prepared_copies`` holds each run with its outcome."""

    def __init__(self, fail, **kw):
        super().__init__(**kw)
        self.fail = fail
        self.prepared_copies = []
        self._prepare_lock = threading.Lock()

    def prepare_stamp(self, path, command, inputs):
        return f'{command}|{inputs}'

    def prepared(self, path):
        return None

    def mark_prepared(self, path, stamp):
        pass

    def prepare_cache(self, project):
        return Path('/agent-home/.cache/devloop-prepare') / project.replace('/', '_')

    def run_prepare(self, path, command, timeout, cache):
        passed = not Path(path).name.endswith(f'-{self.fail}')
        with self._prepare_lock:
            self.prepared_copies.append((Path(path).name, passed))
        return passed, 'npm ERR! code E404'

    def tree(self, path):
        return 't1'

    def tree_changes(self, path, old, new):
        return []


class RunnerCancelTests(unittest.TestCase):
    def test_a_running_turn_is_stopped_with_its_whole_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = agents.AgentRunner(Path(tmp) / 'logs', 60)
            event = threading.Event()
            threading.Timer(0.5, event.set).start()
            script = 'sleep 60 & echo "child $!"; wait'
            started = time.monotonic()
            with mock.patch.object(agents, 'build_command',
                                   return_value=(['bash', '-c', script], None)), \
                    runner.cancellable(event), \
                    self.assertRaises(agents.TurnCancelled) as ctx:
                runner.run(AGENT, 'review', 'p', 'review', tmp, 'pr-100-review')
            self.assertLess(time.monotonic() - started, 10)
            self.assertNotIsInstance(ctx.exception, agents.ModelUnavailable)
            self.assertFalse(ctx.exception.timed_out)
            log = Path(ctx.exception.log).read_text(encoding='utf-8')
            self.assertIn('cancelled', log)
            _wait_gone(self, int(re.search(r'child (\d+)', log).group(1)))

    def test_a_turn_not_started_yet_never_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = mock.Mock()
            runner = agents.AgentRunner(Path(tmp) / 'logs', 60, run=run)
            event = threading.Event()
            event.set()
            with runner.cancellable(event), self.assertRaises(agents.TurnCancelled):
                runner.run(AGENT, 'review', 'p', 'review', tmp, 'pr-100-review')
            run.assert_not_called()
            # Outside the block the turns of this thread run as before.
            self.assertIsNone(getattr(runner._local, 'cancel', None))

    def test_run_in_group_without_cancel_is_unchanged(self):
        done = run_in_group(['bash', '-c', 'echo ok'], timeout=10)
        self.assertEqual((done.returncode, done.stdout), (0, 'ok\n'))
        event = threading.Event()
        threading.Timer(0.2, event.set).start()
        with self.assertRaises(Cancelled):
            run_in_group(['sleep', '30'], timeout=60, cancel=event)


class RereviewTests(Quorum):
    """Criterion 4: a round its first reviewer closed lacks no verdict of its quorum, and the
    supervisor's ``/devloop rereview`` (#491) does not apply to it."""

    def test_a_closed_round_misses_no_verdict(self):
        for mode, missing in (('parallel', [SECOND]), ('sequential', []),
                              ('parallel_cancel', [])):
            with self.subTest(mode=mode):
                self.make(changes(), cfg=with_mode(CFG, mode))
                self.d.tick()
                (pr,) = self.d.snapshot().prs
                self.assertEqual(missing_verdicts(pr), missing)
                self.assertEqual(closed_by_first(pr.reviews), not missing)
                self.assertEqual(pending_reviewers(pr.reviews), missing)

    def test_the_supervisors_rereview_is_refused(self):
        cfg = dataclasses.replace(with_mode(CFG, 'sequential'), merge=MergePolicy(mode='auto'),
                                  supervisor=FIRST)
        self.make(changes(), cfg=cfg)
        history = self.gh.issue_history
        self.gh.issue_history = lambda number: dict(history(number), author='brushtyler')
        self.gh.labeled_events = lambda number: [
            {'label': 'agent:ready', 'by': 'brushtyler', 'at': '2026-11-13T10:00:00Z'}]
        self.d.tick()
        self.assertEqual(self.gh.labels[100], {'agent:changes'})
        # A failure of a review of this head reaches the supervisor all the same.
        (pr,) = self.d.snapshot().prs
        error = agents.AgentError(f'{SECOND} timed out')
        error.reviewer = SECOND
        self.d._escalate_failure(Action('review', 100), pr, error)
        self.assertEqual(self.gh.labels[100], {SUPER})
        supervisor = FakeIdentities.LOGINS[FIRST]
        self.gh.comments.append((100, header() + f'\n/devloop rereview {REASON}'))
        self.gh.written.append(supervisor)
        self.assertEqual(self.d.tick(), [Action('supervise', 100, f'rereview by {supervisor}')])
        self.assertEqual(self.gh.labels[100], {SUPER})
        (refused,) = self.records('supervision')
        self.assertEqual((refused['command'], refused['outcome']), ('rereview', 'refused'))
        body = ' '.join(self.gh.comments[-1][1].split())
        self.assertIn('chiuso dal primo reviewer del quorum', body)
        self.assertIn('non manca alcun verdetto', body)
        self.assertEqual(self.reviewed(), [FIRST])


class DocsTests(unittest.TestCase):
    """Criterion 6: the guide and the reference describe the three modes."""

    def test_the_guide_and_the_reference_describe_the_modes(self):
        root = Path(__file__).resolve().parent.parent
        for name in ('docs/devloop/guide.md', 'docs/devloop/reference.md'):
            text = (root / name).read_text(encoding='utf-8')
            with self.subTest(document=name):
                self.assertIn('second_reviewer', text)
                for mode in policy.SECOND_REVIEWER_MODES:
                    self.assertIn(f'`{mode}`', text)


if __name__ == '__main__':
    unittest.main()
