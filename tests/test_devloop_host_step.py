"""devloop: a PR with a step on the host is merged but not pinned automatically (#462).

The implementer (or a fix) declares ``host_step``; the merge of such a PR holds the auto-pin
(``pin.hold``) and alerts the operator; only a pin by hand (``devloop pin``) releases it. At the
start and at each reload, the tables of the project file the copy reads and the configuration
lacks are reported.
"""

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config, hoststep, markdown, pin
from devloop.config import MergePolicy, PinPolicy
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError
from devloop.schema import load_schema, validate, with_defaults
from helpers import ssdlc_project
from helpers.devloop_fakes import META, FakeGH
from test_devloop import (CFG, REV_NAME, SHA1, FakeRunner, FakeWS, implemented, norm, raw_review,
                          receipt)
from test_devloop_merge import Identities, MergeGH, header, records
from test_devloop_pin import AutoPinTests, FakePinner, GitCase
from test_devloop_zero import changes

STEP = ('Aggiungere `[tests]` al file di progetto e verificare con check-equivalence.py '
        '(ops/host/migrate.md, "Test di ssdlc nel file di progetto")')
ALERT_ISSUE = 50


class HostStepGH(MergeGH):
    """``MergeGH`` whose PR #100 declares ``step`` in its marker (#462); a ``merge_error``
    makes the merge fail."""

    def __init__(self, step=STEP, merge_error=None, lost=False):
        super().__init__()
        self.step, self.merge_error, self.lost = step, merge_error, lost

    def open_prs(self):
        meta = dict(META, host_step=self.step) if self.step else META
        return [dict(raw, body=markdown.publishable('b', 'pr', meta))
                for raw in super().open_prs()]

    def merge_pr(self, number, head):
        if self.merge_error is not None:
            if self.lost:  # GitHub merges, and its answer is lost (a timeout)
                super().merge_pr(number, head)
            raise self.merge_error
        super().merge_pr(number, head)


class StatePinner(pin.AutoPin):
    """``AutoPin`` with its holds and requests in a real ``state_dir``, without a repository:
    nothing is observed, and a pin is only recorded."""

    def __init__(self, root, state):
        super().__init__(root, state)
        self.pins = []

    def observe(self, branch, touches):
        return None

    def pin(self, ref):
        self.pins.append(ref)


class HostStepMergeTests(unittest.TestCase):
    """The merge in ``auto`` of a PR that declares a step on the host."""

    def merge(self, gh, auto=True, alert_issue=ALERT_ISSUE, pinner=None):
        # Not a sensitive path here, so that one level-1 approval suffices for the merge.
        rp = dataclasses.replace(CFG.review_policy, sensitive_paths=())
        cfg = dataclasses.replace(CFG, merge=MergePolicy(mode='auto'), review_policy=rp,
                                  pin=PinPolicy(auto=auto, paths=CFG.pin.paths),
                                  run=config.RunPolicy(alert_issue=alert_issue))
        record = norm(raw_review(), bound=False)
        record['receipt'] = receipt(files=('devloop/config.py',), cfg=cfg)
        gh.thread = []
        gh.say('brushtyler', markdown.render_review(record, header(REV_NAME)))
        ws = FakeWS()
        ws.diff = changes('devloop/config.py')
        self.pinner, self.logs = pinner or FakePinner(), []
        d = Dispatcher(cfg, gh, ws, FakeRunner(), log=self.logs.append,
                       identities=Identities(gh), pinner=self.pinner)
        d.tick()
        return d

    def test_the_merge_holds_the_auto_pin_and_alerts_with_the_step(self):
        gh = HostStepGH()
        d = self.merge(gh)
        # Merged as any other PR: the queue does not stop.
        self.assertEqual(gh.merged, [(100, SHA1)])
        self.assertEqual(len(records(gh, 'merged')), 1)
        # No pin requested: the auto-pin is held with the step.
        self.assertEqual(self.pinner.requests, [])
        self.assertEqual(self.pinner.holds, [(100, SHA1, STEP)])
        self.assertIn('  auto-pin held: #100 declares a step on the host; pin by hand after it',
                      self.logs)
        # The alarm with the step on alert_issue, and the operator's label on the issue.
        alerts = [body for number, body in gh.comments if number == ALERT_ISSUE]
        self.assertEqual(len(alerts), 1)
        self.assertIn('## Passo sull\'host da fare prima del pin', alerts[0])
        self.assertIn('> ' + STEP, alerts[0])
        self.assertIn('#100', alerts[0])
        self.assertIn('`devloop pin`', alerts[0])
        self.assertIn(CFG.labels.needs_operator, gh.labels[META['issue']])
        # Nor a later request: the next tick does not pin while the hold stays.
        self.pinner.request(101, 'b' * 40)
        d._auto_pin(False)
        self.assertEqual(self.pinner.pins, [])
        self.assertIn('auto-pin of origin/main held: the step on the host of #100 waits for the '
                      'operator, who pins by hand after it (devloop pin)', self.logs)

    def test_without_a_step_the_merge_requests_the_pin(self):
        gh = HostStepGH(step='')
        self.merge(gh)
        self.assertEqual(gh.merged, [(100, SHA1)])
        self.assertEqual(self.pinner.requests, [(100, SHA1)])
        self.assertEqual(self.pinner.holds, [])
        self.assertFalse([b for n, b in gh.comments if n == ALERT_ISSUE])
        self.assertNotIn(CFG.labels.needs_operator, gh.labels.get(META['issue'], set()))

    def test_the_step_of_the_latest_fix_holds(self):
        # A fix that declares no step replaces the PR's declaration, and one that declares it
        # replaces an empty one.
        self.assertEqual(hoststep.latest(dict(META, host_step=STEP), [{'host_step': ''}]), '')
        self.assertEqual(hoststep.latest(META, [{'round': 1}, {'host_step': f' {STEP}\n'}]),
                         STEP)
        self.assertEqual(hoststep.latest(dict(META, host_step=STEP), [{'round': 1}]), STEP)
        self.assertEqual(hoststep.latest(META, []), '')
        # A declaration that is not text holds the pin all the same.
        self.assertEqual(hoststep.latest(dict(META, host_step=3), []), hoststep.UNREADABLE)

    def test_a_failed_merge_keeps_its_hold_until_a_tick_reads_the_pr(self):
        gh = HostStepGH(merge_error=GitHubError('merge refused'))
        d = self.merge(gh)
        self.assertEqual(gh.merged, [])
        # The outcome of a failed merge is not known: the hold stays, uncertain.
        self.assertEqual(self.pinner.holds, [(100, SHA1, STEP)])
        self.assertEqual(self.pinner.held()[0]['uncertain'], True)
        self.assertIn('  the merge failed: the hold of the auto-pin stays until a later tick '
                      'reads whether #100 was merged', self.logs)
        # The next tick reads the PR, still open: the hold goes, with no alarm.
        d._auto_pin(False)
        self.assertEqual(self.pinner.holds, [])
        self.assertIn('auto-pin: #100 was not merged: its hold of the auto-pin goes', self.logs)
        self.assertFalse([b for n, b in gh.comments if n == ALERT_ISSUE])

    def test_a_merge_whose_answer_is_lost_keeps_the_hold_and_alerts_later(self):
        # GitHub merges, but the answer is lost: the merge raises as the adapter does on a
        # timeout of `gh pr merge`.
        gh = HostStepGH(merge_error=GitHubError('gh pr merge 100: timed out after 60s'),
                        lost=True)
        d = self.merge(gh)
        self.assertEqual(gh.merged, [(100, SHA1)])
        self.assertEqual(self.pinner.holds, [(100, SHA1, STEP)])
        self.assertFalse([b for n, b in gh.comments if n == ALERT_ISSUE])
        # The next tick reads the PR merged: the hold stays and the alarm goes out.
        self.pinner.request(None, 'b' * 40)
        d._auto_pin(False)
        self.assertEqual(self.pinner.holds, [(100, SHA1, STEP)])
        self.assertNotIn('uncertain', self.pinner.held()[0])
        self.assertEqual(self.pinner.pins, [])
        self.assertIn('auto-pin: #100 was merged despite the error: its step on the host holds '
                      'the auto-pin', self.logs)
        alert, = [b for n, b in gh.comments if n == ALERT_ISSUE]
        self.assertIn('> ' + STEP, alert)
        self.assertIn('#100', alert)
        self.assertIn(CFG.labels.needs_operator, gh.labels[META['issue']])
        # Settled once: a later tick alerts no more, and still does not pin.
        d._auto_pin(False)
        self.assertEqual(len([b for n, b in gh.comments if n == ALERT_ISSUE]), 1)
        self.assertEqual(self.pinner.pins, [])

    def test_a_state_read_for_an_earlier_attempt_does_not_settle_a_later_one(self):
        # Continuous mode: a tick reads the PR open for a failed merge attempt while the merge
        # worker merges it again and loses GitHub's answer. The hold of that new attempt stays.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        pinner = StatePinner(tmp, tmp / 'state')
        gh = HostStepGH(merge_error=GitHubError('merge refused'))
        d = self.merge(gh, pinner=pinner)
        self.assertEqual(gh.merged, [])
        first, = pinner.held()
        self.assertTrue(first['uncertain'])
        read = gh.pr_state

        def worker_merges_again(number):
            answer = read(number)  # OPEN: the first attempt did not merge
            attempt = pinner.hold(100, SHA1, STEP)
            gh.merge_error, gh.lost = GitHubError('gh pr merge 100: timed out after 60s'), True
            with self.assertRaises(GitHubError):
                gh.merge_pr(100, SHA1)
            self.assertTrue(pinner.doubt(100, attempt, META['issue']))
            return answer
        gh.pr_state = worker_merges_again
        d._settle_host_steps()
        self.assertEqual(gh.merged, [(100, SHA1)])
        second, = pinner.held()
        self.assertNotEqual(second['attempt'], first['attempt'])
        self.assertTrue(second['uncertain'])
        self.assertNotIn('auto-pin: #100 was not merged: its hold of the auto-pin goes',
                         self.logs)
        # The next tick reads the new attempt merged: the hold stays, the alarm goes out, and
        # the merged commit, which needs the step on the host, is not pinned.
        gh.pr_state = read
        pinner.request(None, 'b' * 40)
        d._auto_pin(False)
        self.assertEqual(pinner.pins, [])
        self.assertNotIn('uncertain', pinner.held()[0])
        self.assertIn('auto-pin: #100 was merged despite the error: its step on the host holds '
                      'the auto-pin', self.logs)
        alert, = [b for n, b in gh.comments if n == ALERT_ISSUE]
        self.assertIn('> ' + STEP, alert)

    def test_an_unreadable_outcome_keeps_the_hold(self):
        gh = HostStepGH(merge_error=GitHubError('gh pr merge 100: timed out after 60s'))
        d = self.merge(gh)
        gh.pr_state = mock.Mock(side_effect=GitHubError('unreachable'))
        d._auto_pin(False)
        self.assertEqual(self.pinner.holds, [(100, SHA1, STEP)])
        self.assertTrue(self.pinner.held()[0]['uncertain'])
        self.assertIn('auto-pin: whether #100 was merged is not known, next tick: unreachable',
                      self.logs)

    def test_a_hold_that_cannot_be_recorded_stops_the_merge(self):
        pinner = FakePinner()
        pinner.hold = mock.Mock(side_effect=OSError('read-only state_dir'))
        gh = HostStepGH()
        self.merge(gh, pinner=pinner)
        self.assertEqual(gh.merged, [])
        self.assertTrue(any('not merged: it declares a step on the host and the hold of the '
                            'auto-pin could not be recorded' in line for line in self.logs),
                        self.logs)

    def test_with_auto_off_only_the_alarm(self):
        gh = HostStepGH()
        self.merge(gh, auto=False)
        self.assertEqual(gh.merged, [(100, SHA1)])
        self.assertEqual((self.pinner.holds, self.pinner.requests), ([], []))
        alert, = [b for n, b in gh.comments if n == ALERT_ISSUE]
        self.assertIn('Il pin automatico è spento', alert)
        self.assertIn(CFG.labels.needs_operator, gh.labels[META['issue']])

    def test_without_an_alert_issue_it_logs(self):
        gh = HostStepGH()
        self.merge(gh, alert_issue=0)
        self.assertEqual(self.pinner.holds, [(100, SHA1, STEP)])
        self.assertIn('  no [run].alert_issue is configured: no alert for the step on the host',
                      self.logs)


class HeldPinTests(GitCase):
    """The hold on a real repository: the auto-pin waits; a pin by hand releases it and the
    auto-pin resumes."""

    dispatcher = AutoPinTests.dispatcher

    def pin_by_hand(self, ref='main'):
        """``devloop pin <ref>``, the command line of the operator."""
        flat = self.tmp / 'config.toml'
        flat.write_text(ssdlc_project.text(), encoding='utf-8')
        with mock.patch.object(cli, 'main_checkout', return_value=self.root), \
                mock.patch.object(cli, 'log', self.logs.append), \
                mock.patch.object(pin, 'Smoke'):
            cli.main(['--config', str(flat), 'pin', ref])

    def test_a_held_auto_pin_waits_for_a_pin_by_hand_then_resumes(self):
        old = self.pin('main')
        merged = self.push('merged with a step', 'devloop/extra.txt', 'new\n')
        pin.hold(self.state, 100, merged, STEP)
        pin.request(self.state, 100, merged)
        d, snapshots = self.dispatcher(auto=True)
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()  # no pin: the tick plans from the running copy
        self.assertEqual(self.pointed(), old.resolve())
        self.assertEqual(pin.pending(self.state)['pr'], 100)  # the request waits
        self.assertEqual(pin.held(self.state)[0]['step'], STEP)
        self.assertIn('auto-pin of origin/main held: the step on the host of #100 waits for the '
                      'operator, who pins by hand after it (devloop pin)', self.logs)
        # Another merge meanwhile: still no pin.
        later = self.push('merged later', 'devloop/later.txt', 'later\n')
        pin.request(self.state, 101, later)
        with self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()
        self.assertEqual(self.pointed(), old.resolve())
        # The operator does the step, then pins by hand: the hold is released.
        self.pin_by_hand()
        self.assertIsNone(pin.held(self.state))
        self.assertIsNone(pin.pending(self.state))
        self.assertEqual(self.pointed().name, f'pinned-{later[:12]}')
        self.assertIn('auto-pin resumed: the hold after the step on the host of #100 is released',
                      self.logs)
        # The auto-pin resumes with the next merge.
        newer = self.push('merged after the step', 'devloop/extra.txt', 'newer\n')
        pin.request(self.state, 102, newer)
        with self.assertRaises(pin.Repinned) as raised:
            d.tick()
        self.assertEqual(raised.exception.copy, self.state / f'pinned-{newer[:12]}')
        self.assertEqual(snapshots, [1, 1])

    def test_a_merge_while_the_pin_resolves_the_commit_holds_it(self):
        # Continuous mode: the merge worker writes its hold and merges while the tick's auto-pin
        # fetches origin/main. The commit resolved holds that merge, so the pin stops.
        old = self.pin('main')
        before = self.push('merged without a step', 'devloop/extra.txt', 'new\n')
        pin.request(self.state, 101, before)
        d, snapshots = self.dispatcher(auto=True)
        resolve = pin.resolve
        merged = []

        def worker_merges_then_resolve(*args, **kwargs):
            if not merged:
                pin.hold(self.state, 102, SHA1, STEP)
                merged.append(self.push_elsewhere('merged with a step', 'devloop/step.txt',
                                                  'step\n'))
            return resolve(*args, **kwargs)
        with mock.patch.object(pin, 'resolve', side_effect=worker_merges_then_resolve), \
                self.assertRaisesRegex(AssertionError, 'plan'):
            d.tick()
        self.assertEqual(len(merged), 1)
        self.assertEqual(self.pointed(), old.resolve())
        self.assertEqual(pin.pending(self.state)['pr'], 101)  # the request waits
        self.assertEqual(pin.held(self.state)[0]['pr'], 102)
        self.assertIn('auto-pin of origin/main held: the step on the host of #102 waits for the '
                      'operator, who pins by hand after it (devloop pin)', self.logs)
        self.assertEqual(snapshots, [1])

    def test_an_uncertain_hold_is_settled_once(self):
        first = pin.hold(self.state, 100, SHA1, 'first')
        self.assertTrue(pin.doubt(self.state, 100, first, 7))
        self.assertFalse(pin.doubt(self.state, 101, first))
        self.assertEqual(pin.held(self.state)[0]['issue'], 7)
        # Merged: the hold stays, no longer uncertain, and only once.
        self.assertTrue(pin.settle(self.state, 100, True, first))
        self.assertNotIn('uncertain', pin.held(self.state)[0])
        self.assertFalse(pin.settle(self.state, 100, False, first))
        self.assertEqual(pin.held(self.state)[0]['step'], 'first')
        # Not merged: it goes; but not a hold written again by a new merge attempt of the PR,
        # uncertain or not.
        pin.doubt(self.state, 100, first)
        again = pin.hold(self.state, 100, SHA1, 'again')
        self.assertNotEqual(again, first)
        self.assertFalse(pin.doubt(self.state, 100, first))
        self.assertFalse(pin.settle(self.state, 100, False, first))
        pin.doubt(self.state, 100, again)
        self.assertFalse(pin.settle(self.state, 100, False, first))
        self.assertEqual(pin.held(self.state)[0]['step'], 'again')
        self.assertTrue(pin.held(self.state)[0]['uncertain'])
        self.assertTrue(pin.settle(self.state, 100, False, again))
        self.assertIsNone(pin.held(self.state))

    def test_holds_add_up_and_a_failed_merge_drops_only_its_own(self):
        pin.hold(self.state, 100, SHA1, 'first')
        pin.hold(self.state, 101, SHA1, 'second')
        self.assertEqual([s['pr'] for s in pin.held(self.state)], [100, 101])
        pin.unhold(self.state, 101)
        self.assertEqual([s['step'] for s in pin.held(self.state)], ['first'])
        pin.unhold(self.state, 100)
        self.assertIsNone(pin.held(self.state))

    def test_an_unreadable_hold_still_holds(self):
        (self.state).mkdir(parents=True, exist_ok=True)
        (self.state / pin.HELD).write_text('not json', encoding='utf-8')
        self.assertEqual(pin.held_prs(pin.held(self.state)), 'an unknown PR')
        self.assertEqual(len(pin.release(self.state)), 1)
        self.assertIsNone(pin.held(self.state))


class ImplementTests(unittest.TestCase):
    def implement(self, step):
        gh = FakeGH()
        runner = FakeRunner(dict(implemented(), host_step=step))
        Dispatcher(CFG, gh, FakeWS(), runner, log=lambda m: None)._do_implement(7)
        (created,) = gh.created
        return created['body'], runner.calls[0][3]

    def test_the_pr_records_and_shows_the_step(self):
        body, prompt = self.implement(f'  {STEP}\n')
        self.assertEqual(markdown.read_marker(body, 'pr')['host_step'], STEP)
        self.assertIn('### Passo sull\'host', body)
        self.assertIn('> ' + STEP, body)
        self.assertIn('`host_step`', prompt)

    def test_no_step_leaves_the_marker_as_before(self):
        body, _ = self.implement('')
        self.assertNotIn('host_step', markdown.read_marker(body, 'pr'))
        self.assertNotIn('Passo sull\'host', body)


class SchemaTests(unittest.TestCase):
    def test_implementer_and_fixer_declare_host_step(self):
        for name in ('implement', 'fix'):
            with self.subTest(schema=name):
                schema = load_schema(name)
                self.assertIn('host_step', schema['required'])
                self.assertEqual(schema['properties']['host_step'], {'type': 'string'})
                # An output written before #462 declares none.
                out = {'status': 'completed', 'summary': 's', 'commit_message': 'c',
                       'acceptance_map': [], 'blocked_reason': ''}
                out.update({'pr_title': 't', 'pr_body': 'b'} if name == 'implement'
                           else {'responses': []})
                self.assertEqual(validate(with_defaults(name, out), schema)['host_step'], '')

    def test_a_fix_record_carries_the_step(self):
        fix = {'summary': 's', 'responses': [], 'head_sha': SHA1, 'host_step': f'{STEP}\n'}
        body = markdown.render_fix(fix, 'header', 1)
        self.assertEqual(markdown.read_marker(body, 'fix')['host_step'], STEP)
        self.assertIn('### Passo sull\'host', body)
        cleared = markdown.render_fix(dict(fix, host_step=''), 'header', 1)
        self.assertEqual(markdown.read_marker(cleared, 'fix')['host_step'], '')
        self.assertNotIn('Passo sull\'host', cleared)


class PromptTests(unittest.TestCase):
    """The prompts of the implementer, the fixer and the reviewers describe ``host_step``."""

    def test_the_prompts_describe_host_step(self):
        from devloop.context import PROMPT_DIR
        for name in ('implement', 'fix', 'review'):
            text = ' '.join((PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8').split())
            with self.subTest(prompt=name):
                self.assertIn('`host_step`', text)
                self.assertIn('project file', text)
                self.assertIn('`ops/host/`', text)
                self.assertIn('pins by hand', text)
        review = ' '.join((PROMPT_DIR / 'review.md').read_text(encoding='utf-8').split())
        self.assertIn('A declaration missing where it is needed, or present where it is not, is '
                      'a blocking finding', review)
        for name in ('fix', 'review'):
            self.assertIn('$host_step', (PROMPT_DIR / f'{name}.md').read_text(encoding='utf-8'))

    def test_the_reviewer_and_the_fixer_get_the_declaration(self):
        self.assertEqual(hoststep.format_step(''), 'None: the pull request declares no step on '
                                                   'the host (`host_step` is empty).')
        self.assertEqual(hoststep.format_step('a\nb'), '> a\n> b')


class ProjectTablesTests(unittest.TestCase):
    """#462, 3: the tables of the project file this copy reads and the configuration lacks."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.logs, self.comments = [], []
        self.gh = SimpleNamespace(comment=lambda n, body: self.comments.append((n, body)))

    def test_the_missing_tables(self):
        installation = config.read_toml(config.DEFAULT_CONFIG)
        self.assertEqual(config.missing_project_tables(config.compose(installation)),
                         ['prompts', 'tests'])
        project = ssdlc_project.project()
        self.assertEqual(config.missing_project_tables(config.compose(installation, project)),
                         [])
        project.pop('tests')
        self.assertEqual(config.missing_project_tables(config.compose(installation, project)),
                         ['tests'])
        # The overlay may set them too.
        self.assertEqual(config.missing_project_tables(
            config.compose(installation, project,
                           {'tests': {'selection': 'python-unittest-modules'}})), [])

    def test_the_warning_is_logged_each_time_and_published_once(self):
        warn = cli.project_tables_warning(ALERT_ISSUE, self.tmp, self.gh, out=self.logs.append)
        warn(['prompts', 'tests'])
        warn(['prompts', 'tests'])
        self.assertEqual(len([l for l in self.logs if 'reads [prompts], [tests] from the project '
                              'file' in l]), 2)
        (number, body), = self.comments
        self.assertEqual(number, ALERT_ISSUE)
        self.assertIn('## Tabelle del file di progetto assenti', body)
        self.assertIn('`[prompts]`, `[tests]`', body)
        self.assertNotIn(str(self.tmp), body)
        # Another set of missing tables is published again.
        warn(['tests'])
        self.assertEqual(len(self.comments), 2)
        # With none missing the record goes, and a later loss is reported again.
        warn([])
        self.assertFalse((self.tmp / cli.TABLES_ALERTED).exists())
        warn(['tests'])
        self.assertEqual(len(self.comments), 3)

    def test_without_an_alert_issue_or_with_a_failing_comment_it_logs(self):
        cli.project_tables_warning(0, self.tmp, self.gh, out=self.logs.append)(['tests'])
        self.assertEqual(self.comments, [])
        self.assertIn('config: no [run].alert_issue is configured: no alert for the missing '
                      'tables', self.logs)

        def fail(number, body):
            raise RuntimeError('ghs_secret')
        warn = cli.project_tables_warning(ALERT_ISSUE, self.tmp,
                                          SimpleNamespace(comment=fail), out=self.logs.append)
        warn(['tests'])
        self.assertIn(f'config: the alert of the missing tables on #{ALERT_ISSUE} could not be '
                      'published (RuntimeError)', self.logs)
        self.assertFalse(any('ghs_secret' in line for line in self.logs))
        # Not published: tried again at the next call.
        cli.project_tables_warning(ALERT_ISSUE, self.tmp, self.gh, out=self.logs.append)(
            ['tests'])
        self.assertEqual(len(self.comments), 1)

    def start(self, *argv):
        dispatcher = SimpleNamespace(tick=mock.Mock(return_value=[]), ws=mock.Mock(),
                                     config_missing=['prompts', 'tests'], gh=self.gh)
        cfg = SimpleNamespace(run=config.RunPolicy(alert_issue=ALERT_ISSUE))
        with mock.patch.object(cli, 'build', return_value=(cfg, self.tmp, dispatcher)), \
                mock.patch.object(cli, 'log', self.logs.append), \
                mock.patch.dict('os.environ'):
            cli.main(list(argv))
        return dispatcher

    def test_a_copy_without_the_tables_warns_at_the_start(self):
        self.start('tick')
        self.assertTrue(any('config: this copy reads [prompts], [tests] from the project file, '
                            'and the configuration has none' in line for line in self.logs),
                        self.logs)
        (number, body), = self.comments
        self.assertEqual(number, ALERT_ISSUE)
        self.assertEqual(json.loads((self.tmp / cli.TABLES_ALERTED).read_text())['missing'],
                         ['prompts', 'tests'])
        # The next start logs it again, and publishes nothing new.
        self.start('tick')
        self.assertEqual(len(self.comments), 1)

    def test_a_dry_run_writes_nothing(self):
        # The smoke test of a pin runs `tick --dry-run`: no alert from it.
        self.start('tick', '--dry-run')
        self.assertEqual(self.comments, [])

    def test_each_reload_checks_again(self):
        project = self.tmp / 'project.toml'
        project.write_text('test_cmd = "python3 -m unittest"\n', encoding='utf-8')
        # R6: repo and trusted_users are the overlay's.
        local = self.tmp / 'config.local.toml'
        local.write_text('repo = "acme/widget"\ntrusted_users = ["acme-operator"]\n',
                         encoding='utf-8')
        cfg = config.load_config(None, local, project)
        seen = []
        reloader = config.Reloader(cfg, None, local, project, log=self.logs.append,
                                   notice=lambda data: seen.append(
                                       config.missing_project_tables(data)))
        reloader.refresh()
        self.assertEqual(seen, [['prompts', 'tests']])
        reloader.refresh()  # unchanged: not loaded again
        self.assertEqual(len(seen), 1)
        project.write_text('test_cmd = "python3 -m unittest"\n\n[tests]\n'
                           'selection = "python-unittest-modules"\n', encoding='utf-8')
        reloader.refresh()
        self.assertEqual(seen[-1], ['prompts'], self.logs)
        self.assertEqual(len(seen), 2)


if __name__ == '__main__':
    unittest.main()
