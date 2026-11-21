"""devloop: the merge delegated to the supervisor (#486, ADR 0005, precision of 2026-11-21).

``[delegations] merge`` (off by default) lets the supervisor's identity write ``/devloop merge
<sha> <reason>`` on a ready PR whose merge is the operator's (an ``operator_merge``): devloop
merges it with the merger's app, as an automatic merge, only when every condition holds on
``sha``, each verified mechanically just before; otherwise the command is refused with why, and
the PR stays with the operator. The delegated merges have a daily cap of their own (5, a constant
of the code), which ``/devloop merge-budget <N> <reason>`` of a trusted user on ``[report]
issue`` raises for the day it is written.
"""

import dataclasses
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from devloop import config, markdown, report, supervision
from devloop.config import ConfigError, DelegationPolicy, ReportPolicy, parse_delegations
from devloop.dispatch_advisory import advisory_record
from devloop.dispatcher import Dispatcher
from devloop.ledger import Ledger
from devloop.planning import Action
from devloop.review import ci_failure_review
from devloop.workspace import Workspace
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH
from test_devloop import (CFG, REV_NAME, SHA1, SHA2, FakeRunner, FakeWS, norm, raw_review,
                          receipt)
from test_devloop_base_advance import BasePathsTests
from test_devloop_merge import AUTO, SUPERVISOR_LOGIN, header, records
from test_devloop_operator import GitIdentities, NoticeGH
from test_devloop_zero import changes

REPORT = 50  # the issue of the daily summary
DECISIONS = 486  # the issue whose comments record the operator's decisions
TRUSTED_COMMENT, STRANGER_COMMENT = 7054515134, 7054515200
ADR = 'docs/adr/0005-deleghe-e-ruoli-degli-agent.md'
LINK = f'https://github.com/{CFG.repo}/issues/{DECISIONS}#issuecomment-{TRUSTED_COMMENT}'
REASON = ('La PR registra soltanto la decisione dell\'operatore su #486, già scritta.\n'
          'Nessuna decisione nuova.')
NOW = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc).timestamp()
DAY = 24 * 3600
ON = dataclasses.replace(AUTO, delegations=DelegationPolicy(merge=True),
                         report=ReportPolicy(issue=REPORT))
OFF = dataclasses.replace(ON, delegations=DelegationPolicy())
OPERATOR, READY = 'agent:needs-operator', 'agent:ready-to-merge'


def adr_entry(path=ADR, *blocks, removed=False, binary=False):
    """A file of ``Workspace.diff_blocks``."""
    return {'path': path, 'blocks': list(blocks), 'removed': removed, 'binary': binary,
            'other': False}


PRECISION = f'- **Precisazione del 2026-11-21** ([commento]({LINK})): la delega del merge.\n'


class DelegationGH(NoticeGH):
    """``NoticeGH`` (PR #100, approved and ready) whose approval binds the receipt of the diff
    the test gives, with a passing CI, the comments of the issue of the decisions and of the
    summary issue in threads of their own, and the time GitHub gives each comment."""

    def __init__(self, diff, clock):
        super().__init__()
        self.clock = clock
        approval = norm(raw_review())
        approval['receipt'] = receipt(files=diff)
        self.thread = []
        self.say('brushtyler', markdown.render_review(approval, header(REV_NAME)))
        self.details = [{'name': 'Unit Tests', 'bucket': 'pass', 'link': ''}]
        self.others = {DECISIONS: [
            {'id': TRUSTED_COMMENT, 'author': {'login': 'brushtyler'}, 'body': 'Approvato'},
            {'id': STRANGER_COMMENT, 'author': {'login': 'stranger'}, 'body': 'Approvo anche io'}],
            REPORT: []}

    def _stamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _id(self):
        return max([c['id'] for c in self.thread]
                   + [c['id'] for t in self.others.values() for c in t], default=1000) + 1

    def say(self, login, body):
        number = super().say(login, body)
        self.thread[-1]['created_at'] = self._stamp() if hasattr(self, 'clock') else None
        return number

    def say_on(self, number, login, body, at=None):
        self.others[number].append({'id': self._id(), 'author': {'login': login}, 'body': body,
                                    'created_at': at or self._stamp()})

    def comment(self, number, body):
        if number in getattr(self, 'others', {}):
            FakeGH.comment(self, number, body)
            self.say_on(number, 'brushtyler', body)
            return
        super().comment(number, body)

    def issue_comments(self, number):
        if number in self.others:
            return [dict(c) for c in self.others[number]]
        return super().issue_comments(number)


class AdrWS(FakeWS):
    """``FakeWS`` with the changes under ``docs/adr/`` a test gives (``diff_blocks``)."""

    def __init__(self):
        super().__init__(conflicts=())
        self.adr = []
        self.adr_reads = []

    def diff_blocks(self, base, head, prefix):
        self.adr_reads.append((base, head, prefix))
        return [dict(entry) for entry in self.adr]


class Case(unittest.TestCase):
    def make(self, cfg=ON, diff=(ADR,), adr=None, opinion='ok', before=()):
        """PR #100, approved and ready, whose merge the dispatcher told the operator's
        (``operator_merge``), with the advisory review of its head (``opinion``, None for
        none) and a ledger, whose journal counts the delegated merges. ``before``: records
        of the thread written before that notice."""
        self.now = NOW
        self.gh = DelegationGH(diff, lambda: self.now)
        for body in before:
            self.gh.say('brushtyler', body)
        self.ws = AdrWS()
        self.ws.diff = changes(*diff)
        self.ws.adr = [adr_entry(ADR, PRECISION)] if adr is None else adr
        self.logs = []
        d = Dispatcher(cfg, self.gh, self.ws, FakeRunner(), log=self.logs.append,
                       identities=GitIdentities(self.gh))
        d.clock = lambda: self.now
        d.ledger = Ledger(self.gh, clock=lambda: self.now)
        self.assertEqual(d.tick(), [Action('operator_merge', 100, 'merge mode off')])
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        if opinion is not None:
            self.advisory(opinion)
        return d

    def advisory(self, opinion, head=SHA1):
        """The dispatcher's summary of the advisory review of ``head``, with ``opinion``."""
        self.gh.say('brushtyler', markdown.publishable(
            markdown.dispatcher_header() + '\nReview consultiva', 'advisory',
            advisory_record(head, 'claude', opinion)))

    def command(self, text, login=SUPERVISOR_LOGIN):
        return self.gh.say(login, header() + '\n' + text)

    def merge(self, sha=SHA1, reason=REASON, login=SUPERVISOR_LOGIN):
        return self.command(f'/devloop merge {sha} {reason}', login)

    def apply(self, d):
        """Apply the command waiting on PR #100, as its action does."""
        (pr,) = [p for p in d.snapshot().prs if p.number == 100]
        d._do_delegated_merge(pr)

    def decisions(self, number=100):
        found = [markdown.read_marker(c['body'], 'supervision')
                 for c in (self.gh.thread if number == 100 else self.gh.others[number])]
        return [r for r in found if r]

    def text(self):
        """The text of the latest supervision record of PR #100, on one line."""
        (body,) = [c['body'] for c in self.gh.thread
                   if markdown.read_marker(c['body'], 'supervision')][-1:]
        return ' '.join(body.split())

    def assert_refused(self, d, why):
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(records(self.gh, 'merged'), [])
        self.assertEqual(self.decisions()[-1]['outcome'], 'refused')
        self.assertIn(why, self.text())
        self.assertIn(f'la PR resta in `{OPERATOR}`', self.text())
        self.assertEqual(d.ledger.journal.delegated_merges(), [])


class OffTests(Case):
    def test_off_by_default(self):
        self.assertFalse(ssdlc_project.load().delegations.merge)
        self.assertFalse(AUTO.delegations.merge)
        self.assertEqual(parse_delegations({}).max_merges_per_day, 5)
        self.assertEqual(supervision.MAX_DELEGATED_MERGES, 5)

    def test_off_the_command_of_the_supervisor_is_refused(self):
        d = self.make(OFF)
        self.merge()
        self.assertEqual(d.tick(), [Action('delegated_merge', 100, f'merge by {SUPERVISOR_LOGIN}')])
        self.assertEqual(self.gh.merged, [])
        (decision,) = self.decisions()
        self.assertEqual((decision['command'], decision['outcome'], decision['by']),
                         ('merge', 'refused', SUPERVISOR_LOGIN))
        self.assertIn('la delega del merge è spenta (`[delegations] merge`)', self.text())
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        # Spent: nothing else to do.
        self.assertEqual(d.tick(), [])


class MergeTests(Case):
    def test_every_condition_holds_and_the_pr_is_merged_with_the_command_and_its_reason(self):
        d = self.make()
        comment = self.merge()
        self.assertEqual(d.tick(), [Action('delegated_merge', 100, f'merge by {SUPERVISOR_LOGIN}')])
        self.assertEqual(self.gh.merged, [(100, SHA1)])
        (merged,) = records(self.gh, 'merged')
        self.assertEqual((merged['head'], merged['mode'], merged['level']), (SHA1, 'auto', 1))
        self.assertEqual(merged['delegation'], {'command': 'merge', 'by': SUPERVISOR_LOGIN,
                                                'comment': comment, 'reason': REASON})
        body = next(b for _, b in self.gh.comments if markdown.read_marker(b, 'merged'))
        self.assertIn('## Mergiata da devloop su delega del supervisore', body)
        self.assertIn('> Nessuna decisione nuova.', body)
        (decision,) = self.decisions()
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'head', 'daily',
                                                    'daily_cap', 'daily_left', 'opinion')},
                         {'command': 'merge', 'by': SUPERVISOR_LOGIN, 'outcome': 'merged',
                          'head': SHA1, 'daily': 1, 'daily_cap': 5, 'daily_left': 4,
                          'opinion': 'ok'})
        self.assertEqual(decision['reason'], f'{SHA1} {REASON}')
        self.assertIn('Merge delegato al supervisore', self.gh.comments[-1][1])
        (journal,) = d.ledger.journal.delegated_merges()
        self.assertEqual({k: journal[k] for k in ('number', 'comment', 'by', 'head')},
                         {'number': 100, 'comment': comment, 'by': SUPERVISOR_LOGIN,
                          'head': SHA1})
        # A cap of its own: the common grants of the delegations stay untouched.
        self.assertEqual(d.ledger.journal.grants(), [])
        # The link of the ADR block was verified on the comment of a trusted user.
        self.assertEqual(self.ws.adr_reads, [(self.ws.base, SHA1, 'docs/adr/')])

    def test_dubbi_is_enough(self):
        d = self.make(opinion='dubbi')
        self.merge()
        self.apply(d)
        self.assertEqual(self.gh.merged, [(100, SHA1)])

    def test_a_delegated_merge_runs_in_the_merge_lane(self):
        from devloop.planning import MERGE_LANE, schedule
        self.assertIn('delegated_merge', MERGE_LANE)
        actions = [Action('review', 101), Action('delegated_merge', 100), Action('fix', 102)]
        self.assertEqual(len(schedule(actions, {})), 3)


class RefusalTests(Case):
    """Each condition that does not hold refuses the command with why (fails closed)."""

    def test_another_sha(self):
        d = self.make()
        self.merge(sha=SHA2)
        self.assert_refused(d, f'lo sha del comando (`{SHA2[:12]}`) non è l\'head attuale')

    def test_an_abbreviated_sha(self):
        d = self.make()
        self.merge(sha=SHA1[:12])
        self.assert_refused(d, 'con lo sha completo (40 caratteri)')

    def test_an_advisory_review_contrario(self):
        d = self.make(opinion='contrario')
        self.merge()
        self.assert_refused(d, 'ha parere `contrario`: serve `ok` o `dubbi`')

    def test_no_advisory_review(self):
        d = self.make(opinion=None)
        self.merge()
        self.assert_refused(d, f'la review consultiva di `{SHA1[:12]}` manca')

    def test_an_advisory_review_of_another_head(self):
        d = self.make(opinion=None)
        self.advisory('ok', head=SHA2)
        self.merge()
        self.assert_refused(d, 'manca')

    def test_an_open_finding(self):
        d = self.make()
        # A CI round of the same head, whose finding is still open, recorded before the notice
        # (a later review would move the PR on from the operator merge).
        ci = markdown.render_review(ci_failure_review(
            failed_checks=['Unit Tests'], log_tail='boom', previous_open=[], round_no=1,
            max_rounds=3, head_sha=SHA1), markdown.dispatcher_header())
        self.gh.thread.insert(1, {'id': 1001, 'author': {'login': 'brushtyler'}, 'body': ci,
                                  'created_at': None})
        self.merge()
        self.assert_refused(d, 'restano finding bloccanti aperti')

    def test_a_ci_not_green(self):
        d = self.make()
        self.merge()
        self.gh.details = [{'name': 'Unit Tests', 'bucket': 'fail', 'link': ''}]
        self.assert_refused(d, 'la CI dell\'head non è verde (`fail`)')

    def test_a_host_step(self):
        d = self.make()
        self.merge()
        (pr,) = [p for p in d.snapshot().prs if p.number == 100]
        pr.meta['host_step'] = 'Aggiornare l\'overlay.'
        d._do_delegated_merge(pr)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('dichiara un passo sull\'host (`host_step`)', self.text())

    def test_a_change_of_the_trust_rule(self):
        for path in ('ops/host/ssdlc/project.toml', 'devloop/prompts/review.md',
                     'devloop/supervision.py', 'devloop/config.toml'):
            with self.subTest(path=path):
                d = self.make(diff=(ADR, path))
                self.merge()
                self.assert_refused(d, f'la PR tocca la regola di fiducia o le deleghe: `{path}`')

    def test_agents_md_and_claude_md_stay_the_operators(self):
        for path in ('AGENTS.md', 'CLAUDE.md'):
            with self.subTest(path=path):
                d = self.make(diff=(path,))
                self.merge()
                self.assert_refused(d, f'la PR tocca la regola di fiducia o le deleghe: `{path}`')

    def test_the_overlay_may_lengthen_the_rule(self):
        cfg = dataclasses.replace(ON, delegations=DelegationPolicy(
            merge=True, merge_trust_paths=('docs/devloop/',)))
        d = self.make(cfg, diff=(ADR, 'docs/devloop/guide.md'))
        self.merge()
        self.assert_refused(d, '`docs/devloop/guide.md`')

    def test_a_new_decision_in_an_adr(self):
        cases = {
            'a block without a link': (
                [adr_entry(ADR, '- **D11.** Il supervisore mergia tutto.\n')],
                'non ha il link a un commento di questo repository'),
            'a line changed or removed': (
                [adr_entry(ADR, PRECISION, removed=True)], 'non ha solo righe aggiunte'),
            'a binary file': ([adr_entry(ADR, binary=True)], 'non ha solo righe aggiunte'),
            'a link to a comment of an untrusted user': (
                [adr_entry(ADR, PRECISION.replace(str(TRUSTED_COMMENT), str(STRANGER_COMMENT)))],
                'cita solo commenti che non sono di un utente fidato'),
            'a link to a comment that does not exist': (
                [adr_entry(ADR, PRECISION.replace(str(TRUSTED_COMMENT), '123'))],
                'cita solo commenti che non sono di un utente fidato'),
            'a link to another repository': (
                [adr_entry(ADR, PRECISION.replace(CFG.repo, 'someone/else'))],
                'non ha il link a un commento di questo repository'),
            'a change git does not show as text': ([], 'non sono tutte leggibili come testo'),
        }
        for name, (adr, why) in cases.items():
            with self.subTest(case=name):
                d = self.make(adr=adr)
                self.merge()
                self.assert_refused(d, why)

    def test_a_trust_policy_of_the_quorum(self):
        # The reviewer of the quorum says, on the head, that the PR touches the trust policy:
        # its merge is the operator's for that reason too.
        record = norm(raw_review('needs_operator', human='la regola', reason='trust_policy'),
                      round_no=2)
        record['receipt'] = receipt(files=(ADR,))
        d = self.make(before=[markdown.render_review(record, header(REV_NAME))])
        self.assertIn('policy di fiducia', next(b for _, b in self.gh.comments
                                                if 'Il merge spetta' in b))
        self.merge()
        self.assert_refused(d, 'non risulta coperta da una decisione dell\'operatore')

    def test_a_hold_after_the_command_cancels_it(self):
        d = self.make()
        self.merge()
        self.command('/devloop hold aspetto', login='brushtyler')
        self.assert_refused(d, 'un `/devloop hold` scritto dopo il comando lo ha annullato')

    def test_a_merge_mode_other_than_auto(self):
        # Another operator-only path, or a level "off", stays the operator's.
        cfg = dataclasses.replace(ON, merge=config.MergePolicy(mode='auto', levels=((1, 'off'),)))
        d = self.make(cfg, diff=(ADR, 'x.py'))
        self.merge()
        self.assert_refused(d, 'è `off`, non `auto`')

    def test_a_pr_that_is_not_an_operator_merge(self):
        d = self.make()
        d._escalate(100, 'Serve una decisione.', remove=[], cls='failure')
        self.merge()
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('non aspetta il merge dell\'operatore (`operator_merge`)', self.text())


class AuthorTests(Case):
    def test_an_agent_or_an_untrusted_user_is_ignored(self):
        for login in ('codex-agent[bot]', 'someone-else'):
            with self.subTest(login=login):
                d = self.make()
                self.merge(login=login)
                self.assertEqual(d.tick(), [])
                self.assertEqual(self.gh.merged, [])
                self.assertEqual(self.decisions(), [])
                self.assertTrue(any(f'ignored `/devloop merge` by {login}' in line
                                    for line in self.logs))

    def test_a_trusted_user_merges_by_hand(self):
        d = self.make()
        self.merge(login='brushtyler')
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('è una delega del supervisore; l\'operatore mergia la PR a mano',
                      self.text())

    def test_the_command_on_an_escalation_to_the_supervisor_is_refused(self):
        self.assertIn('merge', supervision.COMMANDS)
        self.assertEqual(supervision.supervisor_command(f'/devloop merge {SHA1} perché\nsì'),
                         ('merge', f'{SHA1} perché\nsì'))
        self.assertEqual(supervision.supervisor_command('/devloop merge'), ('merge', None))


class CapTests(Case):
    def fill(self, d, count):
        """``count`` delegated merges of other PRs already today."""
        for i in range(count):
            self.assertIsNotNone(d.ledger.journal.delegated_merge(200 + i, 9000 + i, 'x',
                                                                  SHA2, 99))

    def budget(self, text, login='brushtyler', at=None):
        self.gh.say_on(REPORT, login, header() + '\n' + text, at)

    def test_the_daily_cap_holds(self):
        d = self.make()
        self.fill(d, 5)
        self.merge()
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('oggi i merge delegati hanno già raggiunto il massimo giornaliero (5',
                      self.text())
        self.assertEqual(len(d.ledger.journal.delegated_merges()), 5)

    def test_the_configuration_may_lower_it(self):
        cfg = dataclasses.replace(ON, delegations=DelegationPolicy(merge=True,
                                                                   max_merges_per_day=1))
        d = self.make(cfg)
        self.fill(d, 1)
        self.merge()
        self.apply(d)
        self.assertIn('massimo giornaliero (1', self.text())

    def test_a_trusted_user_raises_the_cap_of_the_day_and_the_next_day_it_is_5_again(self):
        d = self.make()
        self.fill(d, 5)
        self.budget('/devloop merge-budget 2 Molte PR di sole precisazioni degli ADR.')
        self.budget('/devloop merge-budget 1 Ancora una.')
        d.tick()  # the commands on the summary issue are answered at every tick
        granted = [r for r in self.decisions(REPORT) if r['outcome'] == 'granted']
        self.assertEqual([(r['by'], r['value'], r['day'], r['cap']) for r in granted],
                         [('brushtyler', 2, '2026-11-21', 7), ('brushtyler', 1, '2026-11-21', 8)])
        self.assertEqual(granted[0]['reason'], 'Molte PR di sole precisazioni degli ADR.')
        self.assertEqual(sum(e['n'] for e in d._merge_budget_extensions('2026-11-21')), 3)
        self.merge()
        self.apply(d)
        self.assertEqual(self.gh.merged, [(100, SHA1)])
        self.assertEqual(self.decisions()[-1]['daily'], 6)
        self.assertEqual(self.decisions()[-1]['daily_cap'], 8)
        # The next day the extensions of the day before do not count.
        self.now += DAY
        self.assertEqual(d._merge_budget_extensions('2026-11-22'), [])
        self.fill(d, 5)
        self.gh.state, self.gh.merged = 'OPEN', []
        self.merge()
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('massimo giornaliero (5', self.text())

    def test_the_supervisor_or_an_app_does_not_raise_it(self):
        d = self.make()
        self.budget('/devloop merge-budget 3 Le alzo io.', login=SUPERVISOR_LOGIN)
        self.budget('/devloop merge-budget 3 Anche io.', login='codex-agent[bot]')
        d.tick()
        (decision,) = self.decisions(REPORT)
        self.assertEqual((decision['by'], decision['outcome']), (SUPERVISOR_LOGIN, 'refused'))
        self.assertIn('solo un utente fidato', ' '.join(self.gh.others[REPORT][-1]['body'].split()))
        self.assertTrue(any('ignored `/devloop merge-budget` by codex-agent[bot]' in line
                            for line in self.logs))
        self.assertEqual(d._merge_budget_extensions('2026-11-21'), [])

    def test_a_malformed_or_late_extension_is_refused(self):
        for text, at, why in (
                ('/devloop merge-budget 0 x', None, 'da 1 a 20'),
                ('/devloop merge-budget 21 x', None, 'da 1 a 20'),
                ('/devloop merge-budget 2', None, 'da 1 a 20'),
                ('/devloop merge-budget', None, 'da 1 a 20'),
                ('/devloop merge-budget due x', None, 'da 1 a 20'),
                ('/devloop merge-budget 2 ieri', '2026-11-20T23:00:00Z', 'è già finito')):
            with self.subTest(text=text):
                d = self.make()
                self.budget(text, at=at)
                d.tick()
                (decision,) = self.decisions(REPORT)
                self.assertEqual(decision['outcome'], 'refused')
                self.assertIn(why, ' '.join(self.gh.others[REPORT][-1]['body'].split()))
                self.assertEqual(d._merge_budget_extensions('2026-11-21'), [])

    def test_off_an_extension_is_refused(self):
        d = self.make(OFF)
        self.budget('/devloop merge-budget 2 x')
        d.tick()
        (decision,) = self.decisions(REPORT)
        self.assertEqual(decision['outcome'], 'refused')
        self.assertIn('la delega del merge è spenta',
                      ' '.join(self.gh.others[REPORT][-1]['body'].split()))


class ReportTests(unittest.TestCase):
    def test_the_summary_lists_the_delegated_merges_and_the_extensions(self):
        from test_devloop_report import CFG as RCFG, DISPATCHER, ReportGH, TRUST
        gh = ReportGH()
        day = '2026-11-21'

        def supervision(number, at, **record):
            gh.add(number, DISPATCHER, markdown.render_supervision(
                dict({'comment': 1}, **record), 'testo'), at)

        supervision(100, f'{day}T09:00:00Z', command='merge', by=SUPERVISOR_LOGIN,
                    outcome='merged', reason=f'{SHA1} Solo la precisazione dell\'ADR.',
                    head=SHA1, daily=1, daily_cap=5, daily_left=4)
        supervision(REPORT, f'{day}T08:00:00Z', command='merge-budget', by='brushtyler',
                    outcome='granted', reason='Molte precisazioni.', value=2, day=day, cap=7)
        supervision(101, f'{day}T10:00:00Z', command='merge', by=SUPERVISOR_LOGIN,
                    outcome='refused', reason=f'{SHA2} altro')
        when = report.parse_day(day)
        s = report.Records(gh, TRUST, when, when, RCFG.agents).summary(when)
        self.assertEqual(s.delegated, [
            ('08:00', REPORT, 'merge-budget', 'brushtyler', 'Molte precisazioni.', 'concessa',
             f'+2 il {day}: tetto 7'),
            ('09:00', 100, 'merge', SUPERVISOR_LOGIN, f'{SHA1} Solo la precisazione dell\'ADR.',
             'mergiata', 'giorno: 1 di 5'),
            ('10:00', 101, 'merge', SUPERVISOR_LOGIN, f'{SHA2} altro', 'rifiutato', '—')])
        self.assertEqual(s.grants, [])
        text = report.render_text(s)
        self.assertIn('### Merge delegati al supervisore', text)
        self.assertIn('| 09:00 | #100 | `merge` | `agy-agent[bot]` | '
                      f'{SHA1} Solo la precisazione dell\'ADR. | mergiata | giorno: 1 di 5 |', text)
        self.assertIn(f'| 08:00 | #{REPORT} | `merge-budget` | `brushtyler` | Molte precisazioni. '
                      f'| concessa | +2 il {day}: tetto 7 |', text)


class UnitTests(unittest.TestCase):
    def test_the_arguments(self):
        self.assertEqual(supervision.merge_arguments(f'{SHA1} motivo\naltro'),
                         (SHA1, 'motivo\naltro'))
        for text in (SHA1, f'{SHA1[:39]} x', f'{SHA1.upper()} x', '', f'x {SHA1}'):
            with self.subTest(text=text):
                self.assertIsNone(supervision.merge_arguments(text))
        self.assertEqual(supervision.merge_budget_arguments('3 perché'), (3, 'perché'))
        for text in ('0 x', '21 x', '3', '² x', '-1 x', 'tre x', '9' * 50 + ' x'):
            with self.subTest(text=text):
                self.assertIsNone(supervision.merge_budget_arguments(text))

    def test_the_trust_rule_by_path(self):
        self.assertEqual(supervision.TRUST_RULE_PATHS, (
            'AGENTS.md', 'CLAUDE.md', 'devloop/prompts/review.md', 'devloop/prompts/advisory.md',
            'devloop/prompts/preflight.md', 'devloop/policy.py', 'devloop/supervision.py',
            'devloop/context.py', 'ops/host/ssdlc/project.toml'))
        # The file that holds the list is in it.
        self.assertTrue(supervision.__file__.endswith('/devloop/supervision.py'))
        self.assertIn('devloop/supervision.py', supervision.TRUST_RULE_PATHS)
        self.assertEqual(supervision.rule_paths(['x.py', 'docs/adr/a.md', 'AGENTS.md',
                                                 'devloop/config.py', 'docs/x/y.md'],
                                                ('docs/x/',)),
                         ['AGENTS.md', 'devloop/config.py', 'docs/x/y.md'])
        self.assertEqual(supervision.adr_paths(['docs/adr/a.md', 'docs/adrx.md']),
                         ['docs/adr/a.md'])

    def test_the_links_of_an_adr_block(self):
        repo = 'o/r'
        self.assertEqual(supervision.comment_links(
            'vedi https://github.com/o/r/issues/12#issuecomment-34 e '
            'https://github.com/o/r/pull/5#issuecomment-6 e https://github.com/x/r/issues/1'
            '#issuecomment-2', repo), [(12, 34), (5, 6)])
        problems, blocks = supervision.adr_problems([
            adr_entry('docs/adr/a.md', 'https://github.com/o/r/issues/1#issuecomment-2\n'),
            adr_entry('docs/adr/b.md', 'nessun link\n')], repo)
        self.assertEqual(blocks, [('docs/adr/a.md', [(1, 2)])])
        self.assertEqual(len(problems), 1)
        self.assertIn('`docs/adr/b.md`', problems[0])

    def test_the_configuration_may_only_lower_the_cap_and_lengthen_the_rule(self):
        self.assertEqual(parse_delegations({'merge': True, 'max_merges_per_day': 2}),
                         DelegationPolicy(merge=True, max_merges_per_day=2))
        self.assertEqual(parse_delegations({'merge_trust_paths': ['docs/devloop/']})
                         .merge_trust_paths, ('docs/devloop/',))
        for table in ({'max_merges_per_day': 6}, {'merge': 'yes'},
                      {'merge_trust_paths': 'docs/'}, {'merge_trust_paths': ['']}):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                parse_delegations(table)
        data = ssdlc_project.data()
        on = config.apply_overlay(data, {'delegations': {'merge': True},
                                         'review_policy': {'advisory_reviewer': 'claude'}})
        self.assertTrue(config.parse_config(on).delegations.merge)
        with self.assertRaisesRegex(ConfigError, 'advisory_reviewer'):
            config.parse_config(config.apply_overlay(data, {'delegations': {'merge': True}}))
        self.assertIn('delegations', config.LIVE_KEYS)

    def test_a_delegated_merge_record_is_valid(self):
        body = markdown.render_supervision({'command': 'merge', 'by': 'x', 'reason': 'r',
                                            'outcome': 'merged', 'head': SHA1}, 't')
        self.assertEqual(markdown.read_marker(body, 'supervision')['outcome'], 'merged')


class DiffBlocksTests(unittest.TestCase):
    """``Workspace.diff_blocks`` on a real repository: the runs of added lines under a prefix,
    and every other change of a file flagged."""

    git, commit = BasePathsTests.git, BasePathsTests.commit

    def test_the_blocks_of_added_lines_and_the_other_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            self.git('init', '-q', '-b', 'main', str(repo), cwd=tmp)
            base = self.commit(repo, 'root', write=[
                ('docs/adr/a.md', 'uno\ndue\ntre\n'), ('docs/adr/b.md', 'b1\nb2\n'),
                ('docs/adr/c.md', 'c\n'), ('docs/adr/e.bin', 'x'), ('x.py', 'x\n')])
            self.git('checkout', '-q', '-b', 'pr', cwd=repo)
            (repo / 'docs/adr/e.bin').write_bytes(b'\x00\x01\x02')
            (repo / 'docs/adr/link.md').symlink_to('a.md')
            head = self.commit(repo, 'pr', write=[
                ('docs/adr/a.md', 'uno\nprima aggiunta\nseconda\ndue\ntre\nin fondo\n'),
                ('docs/adr/b.md', 'b1\ncambiata\n'), ('docs/adr/d.md', 'nuovo\n'),
                ('x.py', 'y\n')], remove=['docs/adr/c.md'])
            ws = Workspace(repo, '.state', 'main')
            found = {e['path']: e for e in ws.diff_blocks(base, head, 'docs/adr/')}
        self.assertEqual(sorted(found), ['docs/adr/a.md', 'docs/adr/b.md', 'docs/adr/c.md',
                                         'docs/adr/d.md', 'docs/adr/e.bin', 'docs/adr/link.md'])
        self.assertTrue(found['docs/adr/link.md']['other'])  # a symlink is no plain text
        self.assertEqual(found['docs/adr/a.md'],
                         {'path': 'docs/adr/a.md', 'blocks': ['prima aggiunta\nseconda\n',
                                                              'in fondo\n'],
                          'removed': False, 'binary': False, 'other': False})
        self.assertEqual(found['docs/adr/d.md']['blocks'], ['nuovo\n'])
        self.assertFalse(found['docs/adr/d.md']['removed'])
        self.assertTrue(found['docs/adr/b.md']['removed'])
        self.assertTrue(found['docs/adr/c.md']['removed'])
        self.assertTrue(found['docs/adr/e.bin']['binary'])
        problems, blocks = supervision.adr_problems(list(found.values()), 'o/r')
        # The two blocks of a.md and the one of d.md have no link; b, c, e and the link are no
        # added lines of plain text.
        self.assertEqual(len(problems), 7)
        self.assertEqual(blocks, [])


class DocsTests(unittest.TestCase):
    def test_the_delegation_is_documented(self):
        from devloop.context import PROMPT_DIR
        root = PROMPT_DIR.parents[1]

        def text(*parts):
            return ' '.join(root.joinpath(*parts).read_text(encoding='utf-8').split())
        guide = text('docs', 'devloop', 'guide.md')
        self.assertIn('#### Delegated merge', guide)
        self.assertIn('**off by default**: `[delegations] merge = false`', guide)
        self.assertIn('/devloop merge <sha> <reason>', guide)
        self.assertIn('/devloop merge-budget <N> <reason>', guide)
        reference = text('docs', 'devloop', 'reference.md')
        self.assertIn('`max_merges_per_day`', reference)
        self.assertIn('`merge_trust_paths`', reference)
        adr = text('docs', 'adr', '0005-deleghe-e-ruoli-degli-agent.md')
        self.assertIn('issuecomment-7054515134', adr)
        self.assertIn('issuecomment-7035777312', adr)
        self.assertIn('merge delegato', adr)
        toml = (root / 'devloop' / 'config.toml').read_text(encoding='utf-8')
        self.assertIn('merge = false\n', toml)


if __name__ == '__main__':
    unittest.main()
