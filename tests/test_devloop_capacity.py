"""devloop: temporary capacity limits of the supervisor (#412, ADR 0005, P5-8).

``[delegations] capacity`` (off by default) lets the supervisor write ``/devloop capacity <key>
<value> <duration> <reason>`` on ``[report] issue``: ``max_open_prs`` or ``max_parked_prs`` take
``value`` for at most 24 hours, within caps of the code (6 and 6) that the configuration may only
lower, and the configured value is back when it expires. ``max_parallel_actions`` is refused. Each
grant leaves a ``supervision`` record, a ledger ``event`` and a ``grant`` in the turn journal,
which counts against the daily cap of the delegations.
"""

import dataclasses
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from devloop import config, dispatcher, markdown, supervision
from devloop.config import ConfigError, DelegationPolicy, ReportPolicy, parse_delegations
from devloop.github import GitHubError
from devloop.ledger import Ledger
from helpers import ssdlc_project
from test_devloop_grant_round import GrantCase, ci_round
from test_devloop_grant_round import ON as ROUNDS_ON
from test_devloop_supervision import AUTO, SUPER, SUPERVISOR_LOGIN, Case, header

ISSUE = 50  # the issue of the daily summary
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc).timestamp()
WRITTEN = '2026-11-19T09:30:00Z'  # half an hour before NOW
REASON = ('Tre issue piccole e indipendenti sono pronte e gli slot sono pieni da ore.\n'
          'Causa: coda; nessun difetto da correggere.')
ON = dataclasses.replace(AUTO, max_open_prs=2, max_parked_prs=3,
                         report=ReportPolicy(issue=ISSUE),
                         delegations=DelegationPolicy(capacity=True))
OFF = dataclasses.replace(ON, delegations=DelegationPolicy())


class CapacityCase(Case):
    def make(self, cfg=ON, ledger=True):
        d = super().make(cfg)
        d.clock = lambda: self.now
        self.now = NOW
        if ledger:
            d.ledger = Ledger(self.gh)
        return d

    def command(self, text, login=SUPERVISOR_LOGIN, at=WRITTEN):
        """A comment on the summary issue, with the time GitHub gives it."""
        self.gh.say(ISSUE, login, header() + '\n' + text)
        self.gh.threads[ISSUE][-1]['created_at'] = at

    def decisions(self, number=ISSUE):
        return self.gh.records(number, 'supervision')

    def body(self):
        return self.gh.threads[ISSUE][-1]['body']

    def planned(self, d):
        """The configuration a tick plans with (the plan itself runs, its actions do not)."""
        seen, real = [], dispatcher.plan

        def spy(snap, cfg, *args, **kwargs):
            seen.append(cfg)
            real(snap, cfg, *args, **kwargs)
            return []
        with mock.patch.object(dispatcher, 'plan', spy):
            d.tick()
        return seen[-1]


class OffTests(CapacityCase):
    def test_off_by_default(self):
        defaults = ssdlc_project.load().delegations
        self.assertFalse(defaults.capacity)
        self.assertFalse(AUTO.delegations.capacity)

    def test_off_the_command_is_refused_and_spent(self):
        # ADR 0005, §3.1, rule 6: with the delegation off the command is answered "not
        # applicable", and turning the delegation on later grants nothing written before.
        d = self.make(OFF)
        self.command('/devloop capacity max_open_prs 4 6h ' + REASON)
        cfg = self.planned(d)
        self.assertEqual((cfg.max_open_prs, cfg.max_parked_prs), (2, 3))
        (decision,) = self.decisions()
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('la delega dei limiti di capacità è spenta', ' '.join(self.body().split()))
        self.assertIn('Il comando è speso', self.body())
        self.assertEqual(d.ledger.journal.grants(), [])
        d.cfg = ON
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertEqual(len(self.decisions()), 1)
        self.assertEqual(d.ledger.journal.grants(), [])
        # A new command, written once the delegation is on, applies.
        self.command('/devloop capacity max_open_prs 4 6h ' + REASON)
        self.assertEqual(self.planned(d).max_open_prs, 4)
        self.assertEqual([r['outcome'] for r in self.decisions()], ['refused', 'granted'])

    def test_off_a_dry_run_does_not_read_the_issue(self):
        d = self.make(OFF)
        self.command('/devloop capacity max_open_prs 4 6h ' + REASON)
        with mock.patch.object(self.gh, 'issue_comments',
                               side_effect=AssertionError('read')) as read:
            self.assertIs(d._capacity(dry_run=True), OFF)
        read.assert_not_called()
        self.assertEqual(self.decisions(), [])


class GrantTests(CapacityCase):
    def test_a_grant_applies_until_it_expires(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 4 6h ' + REASON)
        cfg = self.planned(d)
        self.assertEqual((cfg.max_open_prs, cfg.max_parked_prs), (4, 3))
        self.assertEqual(d.cfg.max_open_prs, 2)  # the configuration itself never changes
        self.assertTrue(any('/4 in progress' in line for line in self.logs))
        (decision,) = self.decisions()
        self.assertEqual({k: decision[k] for k in (
            'command', 'by', 'outcome', 'setting', 'value', 'configured', 'cap', 'minutes',
            'until', 'daily', 'daily_cap', 'daily_left')},
            {'command': 'capacity', 'by': SUPERVISOR_LOGIN, 'outcome': 'granted',
             'setting': 'max_open_prs', 'value': 4, 'configured': 2, 'cap': 6, 'minutes': 360,
             'until': '2026-11-19T15:30:00Z', 'daily': 1, 'daily_cap': 3, 'daily_left': 2})
        self.assertEqual(decision['reason'], REASON)  # the analysis of the causes, whole
        (trace,) = [c['body'] for c in self.gh.threads[ISSUE]
                    if markdown.read_marker(c['body'], 'supervision')]
        self.assertIn('Alla scadenza torna il valore configurato', trace)
        self.assertIn('> Causa: coda; nessun difetto da correggere.', trace)
        events = [r for r in self.gh.records(ISSUE, 'ledger')
                  if r.get('event') == 'delegation_grant']
        self.assertEqual(len(events), 1)
        self.assertEqual({k: events[0][k] for k in ('delegation', 'by', 'setting', 'value',
                                                    'configured', 'cap', 'until', 'daily')},
                         {'delegation': 'capacity', 'by': SUPERVISOR_LOGIN,
                          'setting': 'max_open_prs', 'value': 4, 'configured': 2, 'cap': 6,
                          'until': '2026-11-19T15:30:00Z', 'daily': 1})
        (grant,) = d.ledger.journal.grants()
        self.assertEqual((grant['delegation'], grant['number'], grant['by']),
                         ('capacity', ISSUE, SUPERVISOR_LOGIN))
        # Still in force, and the command is spent: it is never answered again.
        self.now = NOW + 5 * 3600
        self.assertEqual(self.planned(d).max_open_prs, 4)
        self.assertEqual(len(self.decisions()), 1)
        # Expired: the configured value is back.
        self.now = NOW + 5.5 * 3600
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertIn('capacity: max_open_prs back to the configured 2', self.logs)
        self.assertEqual(len(self.decisions()), 1)

    def test_max_parked_prs_and_minutes(self):
        d = self.make()
        self.command('/devloop capacity max_parked_prs 6 90m ' + REASON)
        cfg = self.planned(d)
        self.assertEqual((cfg.max_open_prs, cfg.max_parked_prs), (2, 6))
        self.assertEqual(self.decisions()[-1]['until'], '2026-11-19T11:00:00Z')

    def test_a_lower_value_applies_too(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 1 2h ' + REASON)
        self.assertEqual(self.planned(d).max_open_prs, 1)

    def test_the_latest_grant_of_a_key_replaces_the_earlier_ones(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 5 20h ' + REASON)
        self.command('/devloop capacity max_open_prs 3 1h ' + REASON)
        self.assertEqual(self.planned(d).max_open_prs, 3)
        self.assertEqual([r['outcome'] for r in self.decisions()], ['granted', 'granted'])
        self.assertEqual(len(d.ledger.journal.grants()), 2)  # each one counts
        self.now = NOW + 2 * 3600
        self.assertEqual(self.planned(d).max_open_prs, 2)  # the earlier one does not come back

    def test_the_revocation_applies_from_the_next_tick(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 4 6h ' + REASON)
        self.assertEqual(self.planned(d).max_open_prs, 4)
        d.cfg = OFF
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertIn('capacity: max_open_prs back to the configured 2', self.logs)

    def test_a_cap_lowered_after_the_grant_stops_it(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 5 6h ' + REASON)
        self.assertEqual(self.planned(d).max_open_prs, 5)
        d.cfg = dataclasses.replace(ON, delegations=DelegationPolicy(capacity=True,
                                                                     capacity_max_open_prs=4))
        self.assertEqual(self.planned(d).max_open_prs, 2)

    def test_a_trusted_user_may_write_it(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 3 1h ' + REASON, login='brushtyler')
        self.assertEqual(self.planned(d).max_open_prs, 3)

    def test_status_shows_the_limits_in_force_and_answers_nothing(self):
        import devloop.__main__ as cli
        d = self.make()
        self.command('/devloop capacity max_open_prs 4 6h ' + REASON)
        self.planned(d)
        self.command('/devloop capacity max_parked_prs 5 6h ' + REASON)
        lines = []
        with mock.patch.object(cli, 'build', return_value=(ON, Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append), \
                mock.patch.object(cli, 'plan', wraps=cli.plan) as planned:
            cli.main(['status'])
        self.assertTrue(any(line.startswith('open PRs: 1/4 in progress') for line in lines),
                        lines)
        self.assertEqual(planned.call_args.args[1].max_open_prs, 4)
        self.assertEqual(len(self.decisions()), 1)  # the later command waits for a tick

    def test_an_unreadable_issue_leaves_the_configured_limits(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 4 6h ' + REASON)
        self.assertEqual(self.planned(d).max_open_prs, 4)
        with mock.patch.object(self.gh, 'issue_comments', side_effect=GitHubError('boom')):
            self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertTrue(any('the configured limits apply' in line for line in self.logs))


class RefusalTests(CapacityCase):
    def refused(self, text, why, cfg=ON, **kwargs):
        d = self.make(cfg, **kwargs)
        self.command(text)
        planned = self.planned(d)
        self.assertEqual((planned.max_open_prs, planned.max_parked_prs),
                         (cfg.max_open_prs, cfg.max_parked_prs))
        self.assertEqual(self.decisions()[-1]['outcome'], 'refused')
        self.assertIn(why, ' '.join(self.body().split()))
        self.assertIn('Il comando è speso', self.body())
        if d.ledger is not None:
            self.assertEqual(d.ledger.journal.grants(), [])
        # Spent: a refused command is answered once.
        self.planned(d)
        self.assertEqual(len(self.decisions()), 1)
        return d

    def test_max_parallel_actions_is_refused(self):
        self.refused('/devloop capacity max_parallel_actions 3 2h ' + REASON,
                     '`max_parallel_actions` non è delegata')

    def test_an_unknown_key(self):
        self.refused('/devloop capacity max_review_rounds 5 2h ' + REASON,
                     'non è fra le chiavi della delega')

    def test_values_above_the_caps_of_the_code(self):
        self.assertEqual(supervision.MAX_CAPACITY, {'max_open_prs': 6, 'max_parked_prs': 6})
        self.refused('/devloop capacity max_open_prs 7 2h ' + REASON,
                     '`max_open_prs` va da 1 a 6')

    def test_a_value_of_zero(self):
        self.refused('/devloop capacity max_parked_prs 0 2h ' + REASON,
                     '`max_parked_prs` va da 1 a 6')

    def test_the_configuration_lowers_the_cap_of_a_key(self):
        cfg = dataclasses.replace(ON, delegations=DelegationPolicy(
            capacity=True, capacity_max_parked_prs=4))
        self.refused('/devloop capacity max_parked_prs 5 2h ' + REASON,
                     '`max_parked_prs` va da 1 a 4 (`[delegations] capacity_max_parked_prs`)',
                     cfg)

    def test_a_duration_above_24_hours(self):
        self.assertEqual(supervision.MAX_CAPACITY_HOURS, 24)
        self.refused('/devloop capacity max_open_prs 4 25h ' + REASON,
                     'supera il massimo di 24 ore')

    def test_the_configuration_lowers_the_duration(self):
        cfg = dataclasses.replace(ON, delegations=DelegationPolicy(capacity=True,
                                                                   capacity_max_hours=2))
        self.refused('/devloop capacity max_open_prs 4 121m ' + REASON,
                     'supera il massimo di 2 ore', cfg)

    def test_without_the_reason(self):
        self.refused('/devloop capacity max_open_prs 4 2h', 'non è ben formato')

    def test_without_anything(self):
        self.refused('/devloop capacity', 'non è ben formato')

    def test_a_malformed_duration(self):
        self.refused('/devloop capacity max_open_prs 4 2d ' + REASON, 'non è ben formato')

    def test_numbers_int_refuses_are_refused_and_the_tick_goes_on(self):
        # str.isdigit accepts ² and int() refuses it, as it refuses thousands of digits: such a
        # command is refused like any malformed one, instead of stopping every tick.
        for text in ('max_open_prs ² 2h', 'max_open_prs 4 ²h', 'max_open_prs ٤ 2h',
                     'max_open_prs ' + '1' * 5000 + ' 2h',
                     'max_open_prs 4 ' + '1' * 5000 + 'm'):
            with self.subTest(text=text[:40]):
                self.refused(f'/devloop capacity {text} ' + REASON, 'non è ben formato')

    def test_a_duration_already_over(self):
        self.refused('/devloop capacity max_open_prs 4 20m ' + REASON, 'è già trascorsa')

    def test_another_command_on_the_summary_issue(self):
        self.refused('/devloop retry', 'sulla issue del riepilogo vale solo `/devloop capacity`')

    def test_outside_merge_mode_auto(self):
        cfg = dataclasses.replace(ON, merge=config.MergePolicy(mode='strict'))
        self.refused('/devloop capacity max_open_prs 4 2h ' + REASON, 'non `auto`', cfg)

    def test_without_a_journal_the_daily_cap_is_not_verifiable(self):
        self.refused('/devloop capacity max_open_prs 4 2h ' + REASON, 'non sono verificabili',
                     ledger=False)

    def test_the_daily_cap_counts_every_delegation(self):
        d = self.make()
        cap = supervision.MAX_DAILY_GRANTS
        for n in range(cap):
            d.ledger.journal.grant('rounds', 200 + n, 9000 + n, SUPERVISOR_LOGIN, cap)
        self.command('/devloop capacity max_open_prs 4 2h ' + REASON)
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertEqual(self.decisions()[-1]['outcome'], 'refused')
        self.assertIn('il massimo giornaliero', self.body())
        self.assertEqual(len(d.ledger.journal.grants()), cap)

    def test_a_command_from_anyone_else_is_ignored(self):
        d = self.make()
        self.command('/devloop capacity max_open_prs 6 24h ' + REASON, login='mallory')
        self.assertEqual(self.planned(d).max_open_prs, 2)
        self.assertEqual(self.decisions(), [])
        self.assertTrue(any('ignored `/devloop capacity` by mallory' in line
                            for line in self.logs))

    def test_a_grant_record_from_anyone_else_is_not_a_limit(self):
        d = self.make()
        self.gh.say(ISSUE, 'mallory', markdown.render_supervision(
            {'command': 'capacity', 'by': SUPERVISOR_LOGIN, 'reason': 'x', 'comment': 1,
             'outcome': 'granted', 'setting': 'max_open_prs', 'value': 6,
             'until': '2026-11-20T00:00:00Z'}, 't'))
        self.assertEqual(self.planned(d).max_open_prs, 2)


class EscalationTests(GrantCase):
    def test_the_command_does_not_apply_on_an_escalation(self):
        d = self.make(ROUNDS_ON)
        self.assertEqual(self.at_limit(d, ci_round())['class'], 'round_grantable')
        self.gh.command(100, '/devloop capacity max_open_prs 4 2h ' + REASON)
        self.assertEqual(self.tick(d), [('supervise', 100)])
        self.assertEqual(self.gh.labels[100], {SUPER})
        self.assertEqual(self.decisions(100)[-1]['outcome'], 'refused')
        self.assertIn('vale solo sulla issue del riepilogo', self.body())
        self.assertEqual(d.ledger.journal.grants(), [])


class ConfigTests(unittest.TestCase):
    def test_the_caps_are_constants_the_configuration_may_only_lower(self):
        self.assertEqual(parse_delegations({}).capacity_cap('max_open_prs'), 6)
        self.assertEqual(parse_delegations({}).capacity_cap('max_parked_prs'), 6)
        self.assertIsNone(parse_delegations({}).capacity_cap('max_parallel_actions'))
        self.assertEqual(parse_delegations({}).capacity_max_hours, 24)
        lowered = parse_delegations({'capacity': True, 'capacity_max_open_prs': 3,
                                     'capacity_max_parked_prs': 1, 'capacity_max_hours': 2})
        self.assertEqual((lowered.capacity, lowered.capacity_cap('max_open_prs'),
                          lowered.capacity_cap('max_parked_prs'), lowered.capacity_max_hours),
                         (True, 3, 1, 2))
        for table in ({'capacity_max_open_prs': 7}, {'capacity_max_parked_prs': 7},
                      {'capacity_max_hours': 25}, {'capacity_max_open_prs': 0},
                      {'capacity_max_hours': True}, {'capacity': 'yes'},
                      {'capacity_max_parallel_actions': 2}):
            with self.subTest(table=table):
                with self.assertRaises(ConfigError):
                    parse_delegations(table)

    def test_the_overlay_is_held_to_the_same_caps_and_needs_the_summary_issue(self):
        data = ssdlc_project.data()
        on =config.apply_overlay(data, {'delegations': {'capacity': True},
                                         'report': {'issue': 9}})
        self.assertTrue(config.parse_config(on).delegations.capacity)
        with self.assertRaises(ConfigError):
            config.parse_config(config.apply_overlay(
                data, {'delegations': {'capacity': True, 'capacity_max_hours': 48},
                       'report': {'issue': 9}}))
        with self.assertRaises(ConfigError) as raised:
            config.parse_config(config.apply_overlay(data, {'delegations': {'capacity': True}}))
        self.assertIn('[report] issue', str(raised.exception))
        self.assertIn('delegations', config.LIVE_KEYS)


class CommandTests(unittest.TestCase):
    def test_parsing(self):
        cmd, args = supervision.supervisor_command, supervision.capacity_arguments
        self.assertEqual(cmd('/devloop capacity max_open_prs 4 6h coda\n\nCausa: coda.'),
                         ('capacity', 'max_open_prs 4 6h coda\n\nCausa: coda.'))
        self.assertEqual(args('max_open_prs 4 6h coda\n\nCausa: coda.'),
                         ('max_open_prs', 4, 360, 'coda\n\nCausa: coda.'))
        self.assertEqual(args('max_parked_prs 6 90m x'), ('max_parked_prs', 6, 90, 'x'))
        self.assertEqual(cmd('/devloop capacity'), ('capacity', None))
        for text in ('max_open_prs 4 6h', 'max_open_prs four 6h x', 'max_open_prs 4 6 x',
                     'max_open_prs 4 0h x', 'max_open_prs 4 1d x', 'max_open_prs -1 6h x', '',
                     'max_open_prs ² 6h x', 'max_open_prs 4 ²h x', 'max_open_prs ٤ 6h x',
                     'max_open_prs ' + '9' * 5000 + ' 6h x',
                     'max_open_prs 4 ' + '9' * 5000 + 'h x',
                     'max_open_prs 1234567890 6h x'):
            with self.subTest(text=text):
                self.assertIsNone(args(text))

    def test_a_capacity_record_is_valid(self):
        body = markdown.render_supervision({'command': 'capacity', 'by': 'x', 'reason': REASON,
                                            'outcome': 'granted', 'setting': 'max_open_prs',
                                            'value': 4, 'until': WRITTEN}, 't')
        self.assertEqual(markdown.read_marker(body, 'supervision')['setting'], 'max_open_prs')


class DocsTests(unittest.TestCase):
    def test_the_delegation_is_documented(self):
        from devloop.context import PROMPT_DIR
        root = PROMPT_DIR.parents[1]

        def text(*parts):
            return ' '.join(root.joinpath(*parts).read_text(encoding='utf-8').split())
        guide = text('docs', 'devloop', 'guide.md')
        self.assertIn('#### Temporary capacity limits', guide)
        self.assertIn('**off by default**: `[delegations] capacity = false`', guide)
        self.assertIn('`max_parallel_actions` is refused', guide)
        reference = text('docs', 'devloop', 'reference.md')
        self.assertIn('`capacity_max_open_prs`', reference)
        protocol = text('docs', 'devloop', 'protocol.md')
        self.assertIn('`/devloop capacity`', protocol)
        toml = (root / 'devloop' / 'config.toml').read_text(encoding='utf-8')
        self.assertIn('capacity = false\n', toml)


if __name__ == '__main__':
    unittest.main()
