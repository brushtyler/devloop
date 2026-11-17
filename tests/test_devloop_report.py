"""devloop: ``devloop report`` (#256), the daily summary from devloop's records, with a simulated
ledger on a fake GitHub."""

import calendar
import contextlib
import dataclasses
import io
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import devloop.__main__ as cli
from devloop import config, ledger, report
from devloop.config import ConfigError, ReportPolicy
from devloop.github import GitHub, GitHubError
from devloop.markdown import publishable, read_marker, render_escalation
from devloop.watch import NOTHING_NEW, Trust

CFG = config.load_config()
TRUST = Trust.from_config(CFG)
DISPATCHER = TRUST.writers['dispatcher']
CLAUDE = TRUST.writers['claude']
CODEX = TRUST.writers['codex']
OPERATOR = CFG.trusted_users[0]
STRANGER = 'someone-else'
ISSUE = 900
RECEIPT = {'pr': 12, 'base': 'b' * 40, 'head': 'a' * 40, 'policy': 'p', 'level': 1,
           'reviewers': ['codex'], 'reasons': []}


def epoch(stamp):
    return calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ'))


class Clock:
    def __init__(self, stamp):
        self.now = epoch(stamp)

    def __call__(self):
        return self.now

    def set(self, stamp):
        self.now = epoch(stamp)


def turn(kind, agent, started, *, state='closed', outcome='success', ms=60000, fallback=None):
    closed = state == 'closed'
    return ledger.render({
        'type': 'turn', 'state': state, 'kind': kind, 'agent': agent, 'identity': None,
        'mode': 'write', 'model': 'm', 'effort': 'high', 'fallback_for': fallback,
        'model_real': None, 'label': None, 'started_at': started,
        'ended_at': started if closed else None, 'duration_ms': ms if closed else None,
        'outcome': outcome if closed else None, 'error': None,
        'telemetry': {} if closed else None})


def event(name, at, **fields):
    return ledger.render(dict(fields, type='event', event=name, at=at))


def review(round_no, reviewer, source='agent'):
    return publishable('review', 'review', {
        'verdict': 'approve', 'round': round_no, 'head_sha': 'a' * 40, 'summary': 's',
        'source': source, 'reviewer': reviewer, 'blocking_findings': [],
        'non_blocking_findings': [], 'previous_findings': []})


def pr_body(implementer='claude'):
    return publishable('body', 'pr', {'issue': 5, 'implementer': implementer,
                                      'reviewer': 'codex'})


class ReportGH:
    """The reads and writes of the report on a simulated repository."""

    repo = CFG.repo

    def __init__(self, clock=None):
        self.clock = clock
        self.comments = []
        self.prs = []
        self.body = ''
        self.author = OPERATOR
        self.editor = None
        self.edited_at = None
        self.calls = []
        self.fail_body = False
        self._next = 1000

    def add(self, number, login, body, created):
        self._next += 1
        self.comments.append({'id': self._next, 'login': login, 'number': number, 'body': body,
                              'created_at': created, 'updated_at': created,
                              'url': f'https://github.com/x/{number}#c{self._next}'})
        return self._next

    def _now(self):
        return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.clock()))

    def dated_comments(self, since=None, number=None):
        self.calls.append(('dated_comments', since, number))
        return [{k: v for k, v in c.items() if k != 'updated_at'} for c in self.comments
                if (number is None or c['number'] == number)
                and (since is None or c['updated_at'] >= since)]

    def search_prs(self, query):
        self.calls.append(('search_prs', query))
        kind, span = query.split(':')
        first, last = span.split('..')
        key = 'createdAt' if kind == 'created' else 'mergedAt'
        return [dict(p) for p in self.prs if p.get(key) and first <= p[key][:10] <= last]

    def issue_history(self, number):
        self.calls.append(('issue_history', number))
        return {'title': 't', 'body': self.body, 'author': self.author, 'editor': self.editor,
                'last_edited_at': self.edited_at, 'labeled': [], 'renamed': []}

    def comment(self, number, body):
        self.calls.append(('comment', number))
        return self.add(number, DISPATCHER, body, self._now())

    def edit_comment(self, comment_id, body):
        self.calls.append(('edit_comment', comment_id))
        for c in self.comments:
            if c['id'] == comment_id:
                c.update(body=body, updated_at=self._now())

    def edit_issue_body(self, number, body):
        self.calls.append(('edit_issue_body', number))
        if self.fail_body:
            raise GitHubError('gh api -X PATCH: HTTP 502')
        self.body, self.editor, self.edited_at = body, DISPATCHER, self._now()

    def archives(self):
        return [read_marker(c['body'], 'report')['day'] for c in self.comments
                if c['number'] == ISSUE and read_marker(c['body'], 'report')]

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


def simulated_day(gh):
    """A day of devloop, 2026-11-16, with records of the day before and forged ones."""
    day = '2026-11-16'
    gh.add(5, DISPATCHER, turn('implement', 'claude', f'{day}T09:12:00Z', ms=750000),
           f'{day}T09:12:01Z')
    gh.add(12, DISPATCHER, turn('review', 'codex', f'{day}T10:00:00Z', ms=95000),
           f'{day}T10:00:01Z')
    gh.add(12, DISPATCHER, turn('fix', 'agy', f'{day}T11:00:00Z', outcome='failure',
                                fallback='gemini-big'), f'{day}T11:00:01Z')
    gh.add(13, DISPATCHER, turn('review', 'codex', f'{day}T23:50:00Z', state='open'),
           f'{day}T23:50:01Z')
    # Started the day before, closed on this day: it belongs to the day before.
    gh.add(7, DISPATCHER, turn('implement', 'claude', '2026-11-15T23:30:00Z', ms=3600000),
           '2026-11-15T23:30:01Z')
    gh.add(12, DISPATCHER, event('ci_round', f'{day}T10:30:00Z', head='c' * 40, state='fail',
                                 checks=[]), f'{day}T10:30:00Z')
    gh.add(12, DISPATCHER, event('ci_round', f'{day}T10:40:00Z', head='c' * 40, state='pass',
                                 checks=[]), f'{day}T10:40:00Z')
    gh.add(12, DISPATCHER, event('ci_rerun', f'{day}T10:31:00Z', head='c' * 40, runs=[42],
                                 modules=['test_x']), f'{day}T10:31:00Z')
    gh.add(14, DISPATCHER, event('sync', f'{day}T12:00:00Z', base='b', head='h'),
           f'{day}T12:00:00Z')
    gh.add(12, DISPATCHER, publishable('merged', 'merged', {
        'head': 'a' * 40, 'level': 1, 'mode': 'auto', 'receipt': RECEIPT}), f'{day}T13:00:00Z')
    gh.add(5, DISPATCHER, render_escalation('r', cls='tests_failed'), f'{day}T14:00:00Z')
    # Round 1 of #12 opened on this day by two reviewers; round 2 of #13 opened the day before.
    gh.add(12, CODEX, review(1, 'codex'), f'{day}T10:05:00Z')
    gh.add(12, CLAUDE, review(1, 'claude'), f'{day}T10:06:00Z')
    gh.add(13, CODEX, review(2, 'codex'), '2026-11-15T22:00:00Z')
    gh.add(13, CLAUDE, review(2, 'claude'), f'{day}T00:10:00Z')
    gh.add(13, DISPATCHER, review(3, 'ci', source='ci'), f'{day}T15:00:00Z')
    # Forged: records not written by the identity that writes them.
    gh.add(5, STRANGER, turn('implement', 'claude', f'{day}T16:00:00Z'), f'{day}T16:00:01Z')
    gh.add(5, CLAUDE, render_escalation('x', cls='forged'), f'{day}T16:00:00Z')
    gh.add(13, STRANGER, review(4, 'codex'), f'{day}T16:00:00Z')
    gh.add(13, CODEX, review(5, 'claude'), f'{day}T16:00:00Z')
    gh.prs = [
        {'number': 12, 'body': pr_body(), 'author': {'login': CLAUDE},
         'createdAt': f'{day}T09:30:00Z', 'mergedAt': f'{day}T13:00:00Z'},
        {'number': 15, 'body': pr_body(), 'author': {'login': CLAUDE},
         'createdAt': '2026-11-14T09:30:00Z', 'mergedAt': f'{day}T17:00:00Z'},
        {'number': 16, 'body': pr_body(), 'author': {'login': STRANGER},
         'createdAt': f'{day}T09:30:00Z', 'mergedAt': None},
        {'number': 17, 'body': 'no marker', 'author': {'login': CLAUDE},
         'createdAt': f'{day}T09:30:00Z', 'mergedAt': None},
    ]
    return report.parse_day(day)


class SummaryTest(unittest.TestCase):
    def summary(self):
        gh = ReportGH()
        day = simulated_day(gh)
        return gh, report.Records(gh, TRUST, day, day, CFG.agents).summary(day)

    def test_the_summary_of_a_day_comes_from_the_records(self):
        gh, s = self.summary()
        self.assertEqual(s.actions, [
            ('09:12', 5, 'implement', 'claude', 'success', 750000),
            ('10:00', 12, 'review', 'codex', 'success', 95000),
            ('11:00', 12, 'fix (fallback)', 'agy', 'failure', 60000),
            ('12:00', 14, 'evento sync', None, None, None),
            ('23:50', 13, 'review', 'codex', 'in corso', None)])
        self.assertEqual(s.merges, [('13:00', 12, 'auto', 1), ('17:00', 15, None, None)])
        self.assertEqual(s.escalations, [('14:00', 5, 'tests_failed', 'operator')])
        self.assertEqual(s.ci, [('10:30', 12, f'blocco: CI `fail` sul commit `{"c" * 12}`'),
                                ('10:31', 12, 'rilancio dei job falliti (run 42)')])
        self.assertEqual(s.opened, [12])
        self.assertEqual(s.merged, [12, 15])
        # Round 1 of #12 and the CI round 3 of #13; round 2 of #13 opened the day before.
        self.assertEqual(s.rounds, 2)
        self.assertEqual(s.fallbacks, 1)
        self.assertEqual(gh.calls[0], ('dated_comments', '2026-11-16T00:00:00Z', None))

    def test_a_turn_belongs_to_the_day_it_started(self):
        gh = ReportGH()
        simulated_day(gh)
        day = report.parse_day('2026-11-15')
        s = report.Records(gh, TRUST, day, day, CFG.agents).summary(day)
        self.assertEqual(s.actions, [('23:30', 7, 'implement', 'claude', 'success', 3600000)])
        self.assertEqual(s.rounds, 1)  # round 2 of #13

    def test_the_rendered_summary(self):
        _, s = self.summary()
        text = report.render_text(s)
        self.assertTrue(text.startswith('# Riepilogo devloop del 2026-11-16 (UTC)'))
        for line in ('| 09:12 | #5 | implement | claude | success | 12m 30s |',
                     '| 11:00 | #12 | fix (fallback) | agy | failure | 1m 00s |',
                     '| 23:50 | #13 | review | codex | in corso | non riportato |',
                     '- 13:00 #12: modalità `auto`, livello 1',
                     '- 17:00 #15: merge di una persona (nessun record di devloop)',
                     '- 14:00 #5: classe `tests_failed`, all\'operatore',
                     '- PR aperte: 1 (#12)', '- PR mergiate: 2 (#12, #15)',
                     '- Round di review aperti: 2', '- Turni con un modello di fallback: 1'):
            self.assertIn(line, text)
        self.assertNotIn('forged', text)

    def test_an_empty_day(self):
        gh = ReportGH()
        day = report.parse_day('2026-11-10')
        s = report.Records(gh, TRUST, day, day, CFG.agents).summary(day)
        self.assertTrue(s.empty())
        self.assertIn('Nessuna attività registrata da devloop.', report.render_text(s))

    def test_durations(self):
        self.assertEqual([report.duration(v) for v in (None, 5000, 750000, 7380000, -1)],
                         ['non riportato', '5s', '12m 30s', '2h 03m', 'non riportato'])


class PublishCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.clock = Clock('2026-11-16T18:00:00Z')
        self.gh = ReportGH(self.clock)
        simulated_day(self.gh)
        self.publisher = report.Publisher(self.gh, TRUST, CFG.repo, ISSUE, agents=CFG.agents,
                                          log=lambda line: None, clock=self.clock,
                                          lock=self.tmp / report.LOCK_FILE)

    def shown(self):
        return read_marker(self.gh.body, 'report')


class PublishTest(PublishCase):
    def test_today_goes_to_the_body_and_a_second_run_edits_it(self):
        self.publisher.publish()
        self.assertEqual(self.shown(), {'event': 'today', 'day': '2026-11-16'})
        self.assertIn('## Riepilogo devloop di oggi: 2026-11-16 (UTC)', self.gh.body)
        self.assertIn('aggiornato alle 18:00 UTC', self.gh.body)
        self.assertIn('- PR mergiate: 2 (#12, #15)', self.gh.body)
        self.assertLess(self.gh.body.index('di oggi'), self.gh.body.index('## Giorni precedenti'))
        self.clock.set('2026-11-16T19:15:00Z')
        self.publisher.publish()
        self.assertIn('aggiornato alle 19:15 UTC', self.gh.body)
        self.assertEqual(self.gh.count('edit_issue_body'), 2)
        self.assertEqual(self.gh.count('comment'), 0)
        self.assertEqual(self.gh.archives(), [])

    def test_the_closed_day_is_archived_once(self):
        self.publisher.publish()
        self.clock.set('2026-11-17T00:20:00Z')
        self.publisher.publish()
        self.assertEqual(self.gh.archives(), ['2026-11-16'])
        archive = next(c for c in self.gh.comments if c['number'] == ISSUE)
        self.assertEqual(archive['login'], DISPATCHER)
        self.assertIn('## Riepilogo devloop del 2026-11-16 (UTC)', archive['body'])
        self.assertIn('- PR mergiate: 2 (#12, #15)', archive['body'])
        self.assertEqual(self.shown(), {'event': 'today', 'day': '2026-11-17'})
        self.assertIn(f'- [2026-11-16](https://github.com/{CFG.repo}/issues/{ISSUE}'
                      f'#issuecomment-{archive["id"]})', self.gh.body)
        # More runs on the new day, the same records: no other archive, no edit of it.
        for stamp in ('2026-11-17T00:40:00Z', '2026-11-17T09:00:00Z'):
            self.clock.set(stamp)
            self.publisher.publish()
        self.assertEqual(self.gh.archives(), ['2026-11-16'])
        self.assertEqual(self.gh.count('comment'), 1)
        self.assertEqual(self.gh.count('edit_comment'), 0)

    def test_a_run_repeated_after_a_failure_does_not_archive_twice(self):
        self.publisher.publish()
        self.clock.set('2026-11-17T00:20:00Z')
        self.gh.fail_body = True
        with self.assertRaises(GitHubError):
            self.publisher.publish()
        self.assertEqual(self.gh.archives(), ['2026-11-16'])
        self.assertEqual(self.shown()['day'], '2026-11-16')  # the body still shows the old day
        self.gh.fail_body = False
        self.publisher.publish()
        self.assertEqual(self.gh.archives(), ['2026-11-16'])
        self.assertEqual(self.shown()['day'], '2026-11-17')

    def test_a_late_record_updates_the_archive_instead_of_adding_one(self):
        self.publisher.publish()
        self.clock.set('2026-11-17T00:20:00Z')
        self.publisher.publish()
        # The body says the old day again (a run with an older copy, say): the archive of that
        # day is reconciled with the records.
        self.gh.add(5, DISPATCHER, render_escalation('late', cls='blocked'),
                    '2026-11-16T23:59:00Z')
        self.gh.body = self.gh.body.replace(
            publishable('', 'report', {'event': 'today', 'day': '2026-11-17'}).strip(),
            publishable('', 'report', {'event': 'today', 'day': '2026-11-16'}).strip())
        self.publisher.publish()
        self.assertEqual(self.gh.archives(), ['2026-11-16'])
        self.assertEqual(self.gh.count('edit_comment'), 1)
        archive = next(c for c in self.gh.comments if c['number'] == ISSUE)
        self.assertIn('classe `blocked`', archive['body'])

    def test_a_turn_closed_after_the_archive_updates_it(self):
        self.publisher.publish()
        # The review of #13 started at 23:50 is still open when its day is archived.
        self.clock.set('2026-11-17T00:01:00Z')
        self.publisher.publish()
        archive = next(c for c in self.gh.comments if c['number'] == ISSUE)
        self.assertEqual(read_marker(archive['body'], 'report'),
                         {'event': 'archive', 'day': '2026-11-16', 'open': True})
        self.assertIn('| 23:50 | #13 | review | codex | in corso |', archive['body'])
        self.assertEqual(self.shown()['day'], '2026-11-17')
        # It closes at 00:05: the dispatcher edits its ledger record.
        record = next(c for c in self.gh.comments if c['number'] == 13
                      and (read_marker(c['body'], 'ledger') or {}).get('state') == 'open')
        record.update(body=turn('review', 'codex', '2026-11-16T23:50:00Z', ms=900000),
                      updated_at='2026-11-17T00:05:00Z')
        self.clock.set('2026-11-17T00:16:00Z')
        self.publisher.publish()
        self.assertEqual(self.gh.archives(), ['2026-11-16'])
        self.assertEqual(self.gh.count('comment'), 1)
        self.assertEqual(self.gh.count('edit_comment'), 1)
        self.assertEqual(read_marker(archive['body'], 'report'),
                         {'event': 'archive', 'day': '2026-11-16', 'open': False})
        self.assertIn('| 23:50 | #13 | review | codex | success | 15m 00s |', archive['body'])
        self.assertNotIn('in corso', archive['body'])
        self.assertNotIn('in corso', self.gh.body)  # not on the new day either
        # No turn of the day is open any more: the next runs leave the archive alone.
        self.gh.calls.clear()
        self.clock.set('2026-11-17T00:31:00Z')
        self.publisher.publish()
        self.assertEqual(self.gh.count('edit_comment'), 0)
        self.assertEqual(self.gh.calls[2], ('dated_comments', '2026-11-17T00:00:00Z', None))

    def test_days_in_between_are_archived_only_with_activity(self):
        self.gh.comments = [c for c in self.gh.comments if c['created_at'] < '2026-11-16']
        self.gh.prs = []
        self.clock.set('2026-11-15T20:00:00Z')
        self.publisher.publish()
        # Nothing on 2026-11-16 nor 2026-11-18, activity on 2026-11-17; today is 2026-11-19.
        self.gh.add(20, DISPATCHER, turn('implement', 'claude', '2026-11-17T08:00:00Z'),
                    '2026-11-17T08:00:01Z')
        self.clock.set('2026-11-19T07:00:00Z')
        self.publisher.publish()
        self.assertEqual(self.gh.archives(), ['2026-11-15', '2026-11-17'])
        self.assertEqual(self.shown()['day'], '2026-11-19')
        index = self.gh.body.split('## Giorni precedenti', 1)[1]
        self.assertLess(index.index('[2026-11-17]'), index.index('[2026-11-15]'))

    def test_markers_count_only_when_the_dispatcher_wrote_them(self):
        forged = publishable('x', 'report', {'event': 'archive', 'day': '2026-11-16'})
        self.gh.add(ISSUE, STRANGER, forged, '2026-11-16T20:00:00Z')
        self.gh.add(ISSUE, CLAUDE, forged, '2026-11-16T20:00:00Z')
        # A body with the record, but last edited by someone else: no day is shown.
        self.gh.body = publishable('x', 'report', {'event': 'today', 'day': '2026-11-10'})
        self.gh.editor, self.gh.edited_at = STRANGER, '2026-11-16T20:00:00Z'
        self.clock.set('2026-11-17T00:20:00Z')
        self.publisher.publish()
        # The forged archive of 2026-11-16 does not count: no catch-up from it, and
        # 2026-11-10 is not archived either.
        self.assertEqual([c['login'] for c in self.gh.comments
                          if c['number'] == ISSUE and c['login'] == DISPATCHER], [])
        self.assertEqual(self.shown(), {'event': 'today', 'day': '2026-11-17'})
        self.assertNotIn('[2026-11-16]', self.gh.body)
        # The next day the body is the dispatcher's: the day it showed is archived.
        self.clock.set('2026-11-18T00:20:00Z')
        self.publisher.publish()
        archived = [c for c in self.gh.comments
                    if c['number'] == ISSUE and c['login'] == DISPATCHER]
        self.assertEqual([read_marker(c['body'], 'report')['day'] for c in archived],
                         ['2026-11-17'])

    def test_a_past_day_updates_only_its_archive_and_the_index(self):
        self.clock.set('2026-11-17T08:00:00Z')
        self.publisher.publish()  # first publication: no day was shown, nothing archived
        self.assertEqual(self.gh.archives(), [])
        head = self.gh.body.split('## Giorni precedenti', 1)[0]
        self.clock.set('2026-11-17T09:00:00Z')
        self.publisher.publish(report.parse_day('2026-11-16'))
        self.assertEqual(self.gh.archives(), ['2026-11-16'])
        self.assertTrue(self.gh.body.startswith(head))  # today's summary kept as it was
        self.assertIn('[2026-11-16]', self.gh.body)
        self.assertEqual(self.shown()['day'], '2026-11-17')
        # Again: the archive is edited only if it changed, never written twice.
        self.publisher.publish(report.parse_day('2026-11-16'))
        self.assertEqual(self.gh.count('comment'), 1)

    def test_a_future_day_is_refused(self):
        with self.assertRaises(report.ReportError):
            self.publisher.publish(report.parse_day('2026-11-17'))
        self.assertEqual(self.gh.count('edit_issue_body') + self.gh.count('comment'), 0)


class AutoReportTest(PublishCase):
    def auto(self, log=None):
        return report.AutoReport(self.publisher, self.tmp, 15, log=log or (lambda line: None),
                                 clock=self.clock)

    def test_the_first_attempt_is_immediate_then_one_per_interval_across_restarts(self):
        self.assertTrue(self.auto().after_tick())
        self.clock.set('2026-11-16T18:10:00Z')
        self.assertFalse(self.auto().after_tick())  # a new process: the interval holds
        self.clock.set('2026-11-16T18:15:00Z')
        self.assertTrue(self.auto().after_tick())
        self.assertEqual(self.gh.count('edit_issue_body'), 2)

    def test_a_failure_is_logged_and_retried_at_the_next_interval(self):
        lines = []
        self.gh.fail_body = True
        auto = self.auto(lines.append)
        self.assertTrue(auto.after_tick())  # no exception: the tick does not fail
        self.assertIn('report: not published (server error)', lines[0])
        self.gh.fail_body = False
        self.clock.set('2026-11-16T18:05:00Z')
        self.assertFalse(auto.after_tick())
        self.clock.set('2026-11-16T18:15:00Z')
        self.assertTrue(auto.after_tick())
        self.assertEqual(self.shown()['day'], '2026-11-16')


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.gh = ReportGH(Clock('2026-11-17T08:00:00Z'))
        simulated_day(self.gh)

    def run_cli(self, argv, cfg=CFG):
        identities = SimpleNamespace(github=lambda actor: self.gh)
        out = io.StringIO()
        with mock.patch.object(cli, 'load_config', return_value=cfg), \
                mock.patch.object(cli.Identities, 'from_config', return_value=identities) as ids, \
                mock.patch.object(cli, 'main_checkout', return_value=self.tmp), \
                mock.patch.object(cli, 'log'), contextlib.redirect_stdout(out):
            cli.main(['report', *argv])
        return out.getvalue(), ids

    def test_report_day_prints_the_summary(self):
        out, _ = self.run_cli(['--day', '2026-11-16'])
        self.assertTrue(out.startswith('# Riepilogo devloop del 2026-11-16 (UTC)'))
        self.assertIn('| 09:12 | #5 | implement | claude | success | 12m 30s |', out)
        self.assertEqual(self.gh.count('edit_issue_body') + self.gh.count('comment'), 0)

    def test_a_malformed_day_is_refused(self):
        for day in ('16-11-2026', '2026-13-01', 'today'):
            with self.subTest(day=day), mock.patch('sys.stderr'), \
                    self.assertRaises(SystemExit) as raised:
                cli.main(['report', '--day', day])
            self.assertEqual(raised.exception.code, 2)

    def test_publish_without_an_issue_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(['--publish'])
        self.assertIn('no [report] issue is configured', str(raised.exception.code))
        self.assertEqual(self.gh.calls, [])

    def test_publish_with_an_issue(self):
        cfg = dataclasses.replace(CFG, report=ReportPolicy(issue=ISSUE))
        with mock.patch.object(report, 'utc_today',
                               return_value=report.parse_day('2026-11-16')):
            self.run_cli(['--publish'], cfg)
        self.assertEqual(read_marker(self.gh.body, 'report'),
                         {'event': 'today', 'day': '2026-11-16'})

    def test_publish_of_a_future_day_fails(self):
        cfg = dataclasses.replace(CFG, report=ReportPolicy(issue=ISSUE))
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(['--publish', '--day', '2999-01-01'], cfg)
        self.assertIn('devloop: report: 2999-01-01 is in the future', str(raised.exception.code))


class RunLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def loop(self, cfg, gh):
        dispatcher = SimpleNamespace(tick=mock.Mock(return_value=[]), gh=gh, identities=None)
        tmp = self.tmp

        class Watcher:
            def __init__(self, *args, **kwargs):
                self.time_left = lambda: 1

            def run(self):
                (tmp / 'STOP').touch()
                return NOTHING_NEW
        with mock.patch.object(cli.watch, 'Watcher', Watcher), mock.patch.object(cli, 'log'):
            return cli.run_loop(cfg, self.tmp, dispatcher, 30)

    def test_without_an_issue_run_publishes_nothing(self):
        gh = mock.Mock(time_left=None)
        with mock.patch.object(report.Publisher, 'publish') as publish:
            self.assertEqual(self.loop(CFG, gh), 0)
        publish.assert_not_called()
        self.assertEqual(gh.method_calls, [])
        self.assertFalse((self.tmp / report.STATE_FILE).exists())

    def test_with_an_issue_run_publishes_after_the_tick(self):
        cfg = dataclasses.replace(CFG, report=ReportPolicy(issue=ISSUE, interval=5))
        with mock.patch.object(report.Publisher, 'publish',
                               side_effect=GitHubError('HTTP 502')) as publish:
            self.assertEqual(self.loop(cfg, mock.Mock(time_left=None)), 0)
        publish.assert_called_once_with()
        self.assertTrue((self.tmp / report.STATE_FILE).exists())


class ConfigTest(unittest.TestCase):
    def test_defaults_and_the_distributed_file(self):
        self.assertEqual(CFG.report, ReportPolicy(issue=0, interval=15))
        self.assertEqual(config.parse_report({}), ReportPolicy())

    def test_values(self):
        self.assertEqual(config.parse_report({'issue': 256, 'interval': 30}),
                         ReportPolicy(issue=256, interval=30))
        for table in ({'issue': 0}, {'issue': True}, {'issue': '5'}, {'interval': 0},
                      {'interval': 1.5}, {'other': 1}, []):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                config.parse_report(table)

    def test_the_overlay_may_set_it(self):
        data = config.apply_overlay(config.load_data(), {'report': {'issue': 300}})
        self.assertEqual(config.parse_config(data).report.issue, 300)


class MarkerAndReadsTest(unittest.TestCase):
    def test_report_marker(self):
        for good in ({'event': 'today', 'day': '2026-11-17'},
                     {'event': 'archive', 'day': '2026-11-16'},
                     {'event': 'archive', 'day': '2026-11-16', 'open': True}):
            self.assertEqual(read_marker(publishable('x', 'report', good), 'report'), good)
        for bad in ({}, {'event': 'other', 'day': '2026-11-17'}, {'event': 'today'},
                    {'event': 'today', 'day': '17/11/2026'},
                    {'event': 'archive', 'day': '2026-11-16', 'open': 'yes'}):
            self.assertIsNone(read_marker(publishable('x', 'report', bad), 'report'))

    def test_github_reads_and_write(self):
        calls = []

        def run(args, **kwargs):
            calls.append((args, kwargs.get('input')))
            out = ('{"id": 7, "login": "x[bot]", "body": "b", '
                   '"created_at": "2026-11-16T10:00:00Z", "url": "u", "number": 3}\n'
                   if args[1] == 'api' else
                   '[{"number": 4, "body": "", "author": {"login": "app/x", "is_bot": true}, '
                   '"createdAt": "c", "mergedAt": null}]')
            return SimpleNamespace(returncode=0, stdout=out, stderr='')

        gh = GitHub('o/r', run=run)
        self.assertEqual(gh.dated_comments(since='2026-11-16T00:00:00Z')[0]['created_at'],
                         '2026-11-16T10:00:00Z')
        self.assertIn('repos/o/r/issues/comments?per_page=100&since=2026-11-16T00:00:00Z',
                      calls[-1][0])
        gh.dated_comments(number=9)
        self.assertIn('repos/o/r/issues/9/comments?per_page=100', calls[-1][0])
        prs = gh.search_prs('created:2026-11-15..2026-11-17')
        self.assertEqual(prs[0]['author'], {'login': 'x[bot]'})
        self.assertIn('created:2026-11-15..2026-11-17', calls[-1][0])
        gh.edit_issue_body(9, 'new body')
        self.assertEqual(calls[-1][0][1:5], ['api', '-X', 'PATCH', 'repos/o/r/issues/9'])
        self.assertEqual(calls[-1][1], 'new body')


if __name__ == '__main__':
    unittest.main()
