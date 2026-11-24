"""devloop: the coordinator's reserved merge (#529, ADR 0002, precision of 2026-11-22).

``/devloop merge <sha> <reason>`` from the coordinator's identity (``[roles.coordinator]``), on a
ready PR whose merge is the operator's (an ``operator_merge``): devloop merges it with the
merger's app when, on ``sha``:

- the conditions of the delegated merge of the supervisor (#486, #509) hold: ``sha`` the current
  head and the approved one, the quorum, no open finding, the CI green, the advisory review
  ``ok`` or ``dubbi``, ``MERGEABLE`` and ``CLEAN``;
- without the exclusions of that delegation for the trust rule, ``docs/adr/`` and the step on
  the host, nor its conditions on ``[delegations] merge`` and the merge mode;
- when a reviewer declared the trust policy touched, or the diff touches ``docs/adr/``, the
  issue the PR closes has a valid ``/devloop approve`` of the operator (``issue_decisions``),
  and each reviewer of the quorum ``operator_reason`` ``decision`` or ``none``;
- within a daily cap of 10, a constant of the code the overlay may only lower, listed in the
  daily summary (#316).

Otherwise the command is refused with why, spent, and the PR stays with the operator.
"""

import dataclasses
import unittest

import test_devloop_delegated_merge as delegated
from devloop import apps, conclave, config, markdown, report, supervision
from devloop.config import ConfigError, DelegationPolicy, MergePolicy
from devloop.journal import COORDINATOR_MERGE, DELEGATED_MERGE, JournalError
from devloop.planning import Action
from devloop.review import ci_failure_review
from helpers import ssdlc_project
from test_devloop import CFG, REV_NAME, SHA1, SHA2, norm, raw_review, receipt
from test_devloop_coordinator import ACTOR, COORDINATOR, TABLE, coordinated, with_coordinator
from test_devloop_delegated_merge import ADR, OFF, OPERATOR, PRECISION, READY, REASON, adr_entry
from test_devloop_merge import MERGER_LOGIN, SUPERVISOR_LOGIN, header, records
from test_devloop_operator import GitIdentities

ISSUE = 7  # the issue PR #100 closes (its ``pr`` marker)
TAKEN = '2026-11-20T11:00:00Z'  # when devloop took the issue on (``agent:working``)
APPROVE = ('/devloop approve Approvo la modifica della regola di fiducia e la precisazione '
           'dell\'ADR.')
APPROVAL = 7099000001  # the id of the operator's comment with the command
# The coordinator's merge needs neither ``[delegations] merge`` nor a merge mode that merges:
# with ``[merge] mode = "off"`` every ready PR is the operator's to merge.
COORD = coordinated(dataclasses.replace(OFF, merge=MergePolicy(mode='off')))
# In merge mode "auto" only a PR on a path reserved to the operator, such as docs/adr/, is.
COORD_AUTO = coordinated(OFF)
TRUST_RECORD = norm(raw_review('needs_operator', human='la regola di fiducia',
                               reason='trust_policy'))


def bound(record, diff):
    """``record`` with the receipt of the diff ``diff``."""
    return dict(record, receipt=receipt(files=diff))


def review_body(record):
    return markdown.render_review(record, header(REV_NAME))


class MergerIdentities(GitIdentities):
    """The identities of the delegated merge tests, plus the merger's app (#201)."""

    def login(self, actor):
        return MERGER_LOGIN if actor == apps.MERGER else super().login(actor)


class Case(delegated.Case):
    def make(self, cfg=COORD, diff=('x.py',), adr=None, opinion='ok', before=(),
             approve=False, merger=True):
        """PR #100, approved and ready, whose merge is the operator's (``delegated.Case``), with
        the history of issue #7, taken on at ``TAKEN``, and its comments; with ``approve`` the
        operator's ``/devloop approve`` written before the takeover. ``merger``: whether devloop
        has the merger's app, which the coordinator's merge requires."""
        d = super().make(cfg, diff, adr, opinion, before)
        if merger:
            d.identities = MergerIdentities(self.gh)
        self.gh.others[ISSUE] = []
        read = self.gh.issue_history

        def history(number):
            found = read(number)
            if number == ISSUE:
                found = dict(found, complete=True, labeled=[
                    ('agent:ready', 'brushtyler', '2026-11-20T10:00:00Z'),
                    ('agent:working', 'brushtyler-devloop-dispatcher[bot]', TAKEN)])
            return found
        self.gh.issue_history = history
        if approve:
            self.approve()
        return d

    def approve(self, login='brushtyler', at='2026-11-20T10:30:00Z', body=APPROVE, edited=None,
                ident=APPROVAL):
        self.gh.others[ISSUE].append({
            'id': ident, 'author': {'login': login}, 'body': body, 'created_at': at,
            'updated_at': edited or at,
            'url': f'https://github.com/{CFG.repo}/issues/{ISSUE}#issuecomment-{ident}'})

    def merge(self, sha=SHA1, reason=REASON, login=COORDINATOR):
        return super().merge(sha, reason, login)

    def merges(self):
        return self.ledger_merges(COORDINATOR_MERGE)

    def ledger_merges(self, event):
        return self.d.ledger.journal.delegated_merges(event=event)

    def assert_merged(self, d):
        self.apply(d)
        self.assertEqual(self.gh.merged, [(100, SHA1)], self.text())
        decision = self.decisions()[-1]
        self.assertEqual((decision['by'], decision['outcome'], decision['role']),
                         (COORDINATOR, 'merged', 'coordinator'))
        return decision

    def assert_refused(self, d, why):
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertEqual(records(self.gh, 'merged'), [])
        decision = self.decisions()[-1]
        self.assertEqual((decision['by'], decision['outcome'], decision['role']),
                         (COORDINATOR, 'refused', 'coordinator'))
        self.assertIn(why, self.text())
        self.assertIn(f'la PR resta in `{OPERATOR}`', self.text())
        self.assertEqual(self.gh.labels[100], {READY, OPERATOR})
        self.assertEqual(d.ledger.journal.delegated_merges(event=COORDINATOR_MERGE), [])

    def apply(self, d):
        self.d = d
        super().apply(d)


class MergeTests(Case):
    """Criterion 1: with every condition true the PR is merged."""

    def test_every_condition_holds_and_the_pr_is_merged(self):
        # ops/host/ssdlc/project.toml is a path of the trust rule: the supervisor's delegation
        # never merges it, the coordinator's merge does. No reviewer declared the trust policy
        # and no ADR is touched: no approval of the operator is needed.
        d = self.make(diff=('x.py', 'ops/host/ssdlc/project.toml'))
        comment = self.merge()
        self.d = d
        self.assertEqual(d.tick(), [Action('delegated_merge', 100, f'merge by {COORDINATOR}')])
        self.assertEqual(self.gh.merged, [(100, SHA1)])
        (merged,) = records(self.gh, 'merged')
        self.assertEqual((merged['head'], merged['mode'], merged['level']), (SHA1, 'auto', 1))
        # Executed by the merger's app (ADR 0002).
        self.assertEqual(merged['merged_by'], MERGER_LOGIN)
        self.assertEqual(merged['delegation'], {'command': 'merge', 'by': COORDINATOR,
                                                'comment': comment, 'reason': REASON,
                                                'role': 'coordinator'})
        body = next(b for _, b in self.gh.comments if markdown.read_marker(b, 'merged'))
        self.assertIn('## Mergiata da devloop su comando del coordinatore', body)
        self.assertIn('**Motivo del coordinatore:**', body)
        self.assertIn('> Nessuna decisione nuova.', body)
        (decision,) = self.decisions()
        self.assertEqual({k: decision[k] for k in ('command', 'by', 'outcome', 'role', 'head',
                                                    'daily', 'daily_cap', 'daily_left',
                                                    'opinion')},
                         {'command': 'merge', 'by': COORDINATOR, 'outcome': 'merged',
                          'role': 'coordinator', 'head': SHA1, 'daily': 1, 'daily_cap': 10,
                          'daily_left': 9, 'opinion': 'ok'})
        self.assertNotIn('approval', decision)
        self.assertIn('Merge riservato del coordinatore', self.gh.comments[-1][1])
        self.assertIn('Merge del coordinatore di oggi: 1 di 10', self.text())
        (journal,) = self.merges()
        self.assertEqual({k: journal[k] for k in ('number', 'comment', 'by', 'head')},
                         {'number': 100, 'comment': comment, 'by': COORDINATOR, 'head': SHA1})
        # Caps of their own: the delegated merges and the grants stay untouched.
        self.assertEqual(self.ledger_merges(DELEGATED_MERGE), [])
        self.assertEqual(d.ledger.journal.grants(), [])

    def test_an_adr_with_the_operators_approval(self):
        # Under docs/adr/, a path reserved to the operator in merge mode "auto" too, any change,
        # not only added lines with a link: the operator approved.
        for cfg in (COORD, COORD_AUTO):
            with self.subTest(mode=cfg.merge.mode):
                d = self.make(cfg, diff=(ADR, 'ops/host/ssdlc/project.toml'),
                              adr=[adr_entry(ADR, PRECISION, removed=True)], approve=True)
                self.merge()
                decision = self.assert_merged(d)
                self.assertEqual(decision['approval'], APPROVAL)
                self.assertIn(f'issuecomment-{APPROVAL} sulla issue #{ISSUE}', self.text())
                self.assertEqual(self.ws.adr_reads, [])

    def test_a_trust_policy_declared_with_the_operators_approval(self):
        # The reviewer declared the trust policy touched in round 1 and approved in round 2.
        diff = ('x.py',)
        d = self.make(diff=diff, approve=True, before=[
            review_body(bound(TRUST_RECORD, diff)),
            review_body(bound(norm(raw_review(), round_no=2), diff))])
        self.merge()
        self.assertEqual(self.assert_merged(d)['approval'], APPROVAL)

    def test_a_host_step(self):
        # The PR is merged; the step on the host holds the auto-pin, as for every merge.
        d = self.make()
        self.merge()
        (pr,) = [p for p in d.snapshot().prs if p.number == 100]
        pr.meta['host_step'] = 'Aggiornare l\'overlay (ops/host/migrate.md).'
        self.d = d
        d._do_delegated_merge(pr)
        self.assertEqual(self.gh.merged, [(100, SHA1)])
        self.assertEqual(self.decisions()[-1]['outcome'], 'merged')

    def test_any_merge_mode_and_the_delegation_off(self):
        for merge in (MergePolicy(mode='off'), MergePolicy(mode='strict', levels=((1, 'off'),)),
                      MergePolicy(mode='auto', levels=((1, 'off'),))):
            with self.subTest(merge=merge):
                cfg = dataclasses.replace(COORD, merge=merge,
                                          delegations=DelegationPolicy(merge=False))
                d = self.make(cfg, diff=('x.py',))
                self.merge()
                self.assert_merged(d)

    def test_dubbi_is_enough(self):
        d = self.make(opinion='dubbi')
        self.merge()
        self.assert_merged(d)


class RefusalTests(Case):
    """Criterion 1: each condition that does not hold refuses the command with why."""

    def test_another_sha(self):
        d = self.make()
        self.merge(sha=SHA2)
        self.assert_refused(d, f'lo sha del comando (`{SHA2[:12]}`) non è l\'head attuale')

    def test_an_abbreviated_sha(self):
        d = self.make()
        self.merge(sha=SHA1[:12])
        self.assert_refused(d, 'con lo sha completo (40 caratteri)')

    def test_a_head_not_approved(self):
        # The latest round of the head asks for the operator: the head is not the approved one.
        record = bound(norm(raw_review('needs_operator', human='serve una scelta',
                                       reason='decision'), round_no=2), ('x.py',))
        d = self.make()
        self.gh.thread.insert(1, {'id': 1001, 'author': {'login': 'brushtyler'},
                                  'body': review_body(record), 'created_at': None})
        self.merge()
        self.assert_refused(d, f'lo sha `{SHA1[:12]}` non è quello approvato dall\'ultimo round')

    def test_an_open_finding(self):
        d = self.make()
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

    def test_not_mergeable_or_not_clean(self):
        for mergeable, status in (('CONFLICTING', 'DIRTY'), ('MERGEABLE', 'BLOCKED')):
            with self.subTest(status=status):
                d = self.make()
                self.merge()
                self.gh.mergeable, self.gh.status = mergeable, status
                self.assert_refused(d, 'GitHub non riporta la PR come `MERGEABLE` e `CLEAN`')

    def test_an_advisory_review_contrario_or_missing(self):
        for opinion, why in (('contrario', 'ha parere `contrario`: serve `ok` o `dubbi`'),
                             (None, f'la review consultiva di `{SHA1[:12]}` manca')):
            with self.subTest(opinion=opinion):
                d = self.make(opinion=opinion)
                self.merge()
                self.assert_refused(d, why)

    def test_no_quorum_review_of_the_sha(self):
        # The quorum reviewer's latest review of the head says trust_policy: its operator_reason
        # is neither decision nor none, even with the operator's approval.
        record = bound(norm(raw_review('needs_operator', human='la regola',
                                       reason='trust_policy'), round_no=2), ('x.py',))
        d = self.make(before=[review_body(record)], approve=True)
        self.merge()
        self.assert_refused(d, 'non risulta coperta da una decisione dell\'operatore')

    def test_a_trust_policy_declared_without_the_operators_approval(self):
        diff = ('x.py',)
        d = self.make(diff=diff, before=[
            review_body(bound(TRUST_RECORD, diff)),
            review_body(bound(norm(raw_review(), round_no=2), diff))])
        self.merge()
        self.assert_refused(d, 'un reviewer ha dichiarato toccata la policy di fiducia, e la '
                               f'issue #{ISSUE} che la PR chiude non ha un `/devloop approve` '
                               'valido dell\'operatore')

    def test_an_adr_without_the_operators_approval(self):
        # Even an ADR change the supervisor's delegation would admit: a precision with the link.
        d = self.make(diff=(ADR,))
        self.merge()
        self.assert_refused(d, f'il diff tocca `docs/adr/`, e la issue #{ISSUE} che la PR '
                               'chiude non ha un `/devloop approve` valido')

    def test_an_approval_that_does_not_count(self):
        cases = {
            'written after the takeover': dict(at='2026-11-20T11:30:00Z'),
            'edited after the takeover': dict(edited='2026-11-20T12:00:00Z'),
            'of an app': dict(login=COORDINATOR),
            'of the supervisor': dict(login=SUPERVISOR_LOGIN),
            'of an untrusted user': dict(login='stranger'),
            'with the attribution header': dict(body=header() + '\n' + APPROVE),
            'without the text of the decision': dict(body='/devloop approve'),
            'not a command': dict(body='Approvo.'),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                d = self.make(diff=(ADR,))
                self.approve(**kwargs)
                self.merge()
                self.assert_refused(d, 'non ha un `/devloop approve` valido dell\'operatore')

    def test_an_issue_whose_text_changed_after_the_takeover(self):
        d = self.make(diff=(ADR,), approve=True)
        self.gh.edits = ('2026-11-21T09:00:00Z', 'brushtyler')
        self.merge()
        self.assert_refused(d, 'non ha un `/devloop approve` valido dell\'operatore')

    def test_without_the_merger_app(self):
        # A configuration without [apps.merger] is valid, and devloop would merge with the
        # ambient gh login: the coordinator's merge is refused instead, before the cap.
        d = self.make(merger=False)
        self.merge()
        self.assert_refused(d, 'il merge del coordinatore si esegue solo con l\'app merger, e '
                               'devloop non ce l\'ha (`[apps.merger]`)')

    def test_a_hold_after_the_command_cancels_it(self):
        d = self.make()
        self.merge()
        self.command('/devloop hold aspetto', login='brushtyler')
        self.assert_refused(d, 'un `/devloop hold` scritto dopo il comando lo ha annullato')

    def test_a_merge_hold_label(self):
        d = self.make()
        self.merge()
        self.gh.labels[100].add('agent:merge-hold')
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('le label della PR non sono più quelle di un merge dell\'operatore',
                      self.text())

    def test_a_pr_that_is_not_an_operator_merge(self):
        d = self.make()
        d._escalate(100, 'Serve una decisione.', remove=[], cls='failure')
        self.merge()
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('non aspetta il merge dell\'operatore (`operator_merge`)', self.text())

    def test_on_an_escalation_to_the_supervisor(self):
        # As the supervisor's: a merge applies only to a PR whose merge is the operator's.
        from test_devloop_coordinator import EscalationCase
        from test_devloop_supervision import AUTO, SUPER, crash

        class Escalation(EscalationCase):
            def runTest(self):
                pass
        case = Escalation()
        d = case.make(coordinated(AUTO), crash(), raw_review())
        case.fail_review(d, SUPER)
        case.gh.command(100, f'/devloop merge {SHA1} motivo', login=COORDINATOR)
        self.assertEqual([(a.kind, a.number) for a in d.tick()], [('delegated_merge', 100)])
        self.assertIn('vale solo su una PR pronta il cui merge spetta all\'operatore',
                      case.answer('merge'))
        self.assertEqual(case.gh.labels[100], {SUPER})


def _profile(i):
    return {'identity': f'app-{i}', 'kind': 'k', 'model': f'model-{i}', 'effort': 'high',
            'args': []}


def conclave_body(issue_hash, step='review', head=SHA1, round_no=1, resumed=True):
    """The dispatcher's record of a ``UNANIMOUS`` conclave decision on issue #7 (#142), taken on
    the request whose hash is ``issue_hash``, whose work restarted (``resumed``): on a review
    block of round ``round_no``, or on a stopped ``implement`` or ``fix``."""
    block = ({'kind': 'review', 'round': round_no, 'reviewer': REV_NAME, 'head': head}
             if step == 'review' else
             {'kind': 'blocked', 'comment': 7000000001, 'agent': 'claude', 'step': step})
    record = conclave.new_record(block, step, ['claude', 'codex', 'agy'], ISSUE, issue_hash,
                                 SHA1, head, 'd' * 64)
    conclave.apply_proposal(record, {'reserved': False, 'resolution': 'Usare il formato JSON.',
                                     'reserved_reason': '', 'operator_question': '',
                                     'options': []}, _profile(0))
    for index in (0, 1):
        conclave.apply_vote(record, index, {'vote': conclave.APPROVE, 'reason': 'Coerente.',
                                            'reserved': False, 'reserved_reason': '',
                                            'operator_question': '', 'options': []},
                            record['hash'], _profile(index + 1))
    record['outcome'] = conclave.outcome(record)
    if resumed:
        record['resumed'] = {'evidence': None}
    return conclave.render(record)


class ConclaveTests(Case):
    """#624: after a conclave restarted the work, the coordinator's reserved merge only, when
    the decision was unanimous and still holds and a complete round after the restart approved
    the head."""

    def request(self):
        """The hash of the request of issue #7 as the dispatcher reads it now."""
        return self.make()._issue_context(ISSUE)[0]['request']

    def reviewed(self, *after, issue_hash=None, cfg=COORD):
        """PR #100 whose review block of round 1 a unanimous conclave restarted, then ``after``,
        before the ``operator_merge``."""
        decision = conclave_body(issue_hash or self.request())
        return self.make(cfg, before=[decision, *after])

    def round_two(self):
        return review_body(bound(norm(raw_review(), round_no=2), ('x.py',)))

    def test_a_round_after_the_restart_and_the_pr_is_merged(self):
        for cfg in (COORD, COORD_AUTO):
            with self.subTest(mode=cfg.merge.mode):
                d = self.reviewed(self.round_two(), cfg=cfg)
                (cid,) = [c['id'] for c in self.gh.thread
                          if markdown.read_marker(c['body'], 'conclave')]
                self.assertEqual(d._human_merge(d.snapshot().prs[0]), cid)
                self.merge()
                decision = self.assert_merged(d)
                self.assertEqual(decision['conclave'], cid)
                self.assertIn(f'decisione unanime del conclave {cid}', self.text())

    def test_devloop_alone_never_merges_it(self):
        # Merge mode "auto" would merge this PR: after the restart it is the operator's, and
        # the final check of devloop's own merge refuses it too.
        d = self.reviewed(self.round_two(), cfg=COORD_AUTO)
        (pr,) = d.snapshot().prs
        self.assertEqual(pr.merge_mode, 'off')
        d._do_merge(dataclasses.replace(pr, merge_mode='auto'))
        self.assertEqual(self.gh.merged, [])
        self.assertIn('o il coordinatore con il suo merge riservato',
                      d._operator_merge_reasons(pr))

    def test_a_restarted_implementation_and_the_pr_is_merged(self):
        # The decision is on the issue: the PR was opened after the restart, and its rounds,
        # written after the decision, count.
        hash_ = self.request()
        d = self.make()
        self.gh.say_on(ISSUE, 'brushtyler', conclave_body(hash_, step='implement', head=None),
                       at='2026-11-20T10:45:00Z')
        (cid,) = [c['id'] for c in self.gh.others[ISSUE]
                  if markdown.read_marker(c['body'], 'conclave')]
        self.merge()
        self.assertEqual(self.assert_merged(d)['conclave'], cid)

    def restarted_on_the_issue(self, round_two=False, edited=None):
        """PR #100, approved at ``NOW`` (10:00), and a unanimous decision on issue #7 written
        at 10:01, that restarted its implementation (a PR closed and reopened), its comment
        changed at ``edited``; with ``round_two``, a second round written at 10:02."""
        hash_ = self.request()
        d = self.make(before=[self.round_two()] if round_two else [])
        if round_two:
            (review,) = [c for c in self.gh.thread
                         if (markdown.read_marker(c['body'], 'review') or {}).get('round') == 2]
            review['created_at'] = '2026-11-21T10:02:00Z'
        self.gh.say_on(ISSUE, 'brushtyler', conclave_body(hash_, step='implement', head=None),
                       at='2026-11-21T10:01:00Z')
        if edited is not None:
            self.gh.others[ISSUE][-1]['updated_at'] = edited
        return d

    def test_approvals_before_a_restart_on_the_issue(self):
        d = self.restarted_on_the_issue()
        self.merge()
        self.assert_refused(d, 'nessun round di review completo dopo la ripartenza del lavoro')

    def test_a_round_after_a_restart_on_the_issue(self):
        d = self.restarted_on_the_issue(round_two=True)
        self.merge()
        self.assert_merged(d)

    def test_the_restart_on_the_issue_is_its_latest_change(self):
        # The dispatcher writes ``resumed`` in the decision as the work restarts: a round
        # written before that change does not count.
        d = self.restarted_on_the_issue(round_two=True, edited='2026-11-21T10:05:00Z')
        self.merge()
        self.assert_refused(d, 'nessun round di review completo dopo la ripartenza del lavoro')

    def test_a_restart_on_the_issue_without_a_readable_time(self):
        d = self.restarted_on_the_issue(round_two=True, edited='ieri')
        self.merge()
        self.assert_refused(d, 'l\'ora di una decisione del conclave della issue #7')

    def test_no_round_after_the_restart(self):
        d = self.reviewed()
        self.merge()
        self.assert_refused(d, 'nessun round di review completo dopo la ripartenza del lavoro')

    def test_a_vetoed_decision(self):
        d = self.reviewed(self.round_two())
        self.command('/devloop veto La direzione non è quella dell\'issue.', login='brushtyler')
        self.merge()
        self.assert_refused(d, '`brushtyler` ha annullato con `/devloop veto`')

    def test_a_lapsed_decision(self):
        d = self.reviewed(self.round_two(), issue_hash='e' * 64)
        self.merge()
        self.assert_refused(d, 'è decaduta: la richiesta della issue #7 è cambiata')

    def test_a_later_unanimous_decision_not_restarted(self):
        d = self.reviewed(self.round_two())
        request = d._issue_context(ISSUE)[0]['request']
        self.gh.say('brushtyler', conclave_body(request, round_no=2, resumed=False))
        self.merge()
        self.assert_refused(d, 'non è più l\'ultima decisione unanime')

    def test_the_supervisors_delegation_still_refuses_it(self):
        cfg = dataclasses.replace(COORD_AUTO, delegations=DelegationPolicy(merge=True))
        d = self.reviewed(self.round_two(), cfg=cfg)
        super(Case, self).merge(SHA1, REASON, SUPERVISOR_LOGIN)
        delegated.Case.apply(self, d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('il merge resta umano', self.text())


class CapTests(Case):
    """Criterion 2: at most 10 reserved merges per UTC day, a cap the overlay may only lower."""

    def fill(self, d, count, event=COORDINATOR_MERGE):
        for i in range(count):
            self.assertIsNotNone(d.ledger.journal.delegated_merge(200 + i, 9000 + i, 'x', SHA2,
                                                                  99, event))

    def test_the_cap_is_a_constant_of_the_code(self):
        self.assertEqual(supervision.MAX_COORDINATOR_MERGES, 10)
        self.assertEqual(ACTOR.max_merges_per_day, 10)

    def test_the_daily_cap_holds(self):
        d = self.make()
        self.fill(d, 10)
        self.merge()
        self.apply(d)
        self.assertEqual(self.gh.merged, [])
        self.assertIn('oggi i merge del coordinatore hanno già raggiunto il massimo giornaliero '
                      '(10: `[roles.coordinator] max_merges_per_day` = 10, al massimo 10)',
                      self.text())
        self.assertEqual(len(self.ledger_merges(COORDINATOR_MERGE)), 10)

    def test_the_delegated_merges_do_not_count(self):
        d = self.make()
        self.fill(d, 5, DELEGATED_MERGE)
        self.merge()
        self.assertEqual(self.assert_merged(d)['daily'], 1)

    def test_the_next_day_it_starts_again(self):
        d = self.make()
        self.fill(d, 10)
        self.now += delegated.DAY
        self.merge()
        self.assertEqual(self.assert_merged(d)['daily'], 1)

    def test_the_overlay_may_lower_it(self):
        cfg = dataclasses.replace(COORD, coordinator=dataclasses.replace(
            ACTOR, max_merges_per_day=1))
        d = self.make(cfg)
        self.fill(d, 1)
        self.merge()
        self.apply(d)
        self.assertIn('massimo giornaliero (1: `[roles.coordinator] max_merges_per_day` = 1',
                      self.text())

    def test_zero_turns_it_off(self):
        cfg = dataclasses.replace(COORD, coordinator=dataclasses.replace(
            ACTOR, max_merges_per_day=0))
        d = self.make(cfg)
        self.merge()
        self.assert_refused(d, 'il merge del coordinatore è spento')

    def test_a_journal_that_cannot_be_read(self):
        d = self.make()
        self.merge()

        def unreadable(*args):
            raise JournalError('journal.jsonl: not readable')
        d.ledger.journal.delegated_merge = unreadable
        self.assert_refused(d, 'i merge del coordinatore di oggi non sono verificabili')

    def test_the_key_of_the_overlay(self):
        def parsed(cap):
            table = dict(TABLE, max_merges_per_day=cap)
            return config.parse_config(with_coordinator(table)).coordinator
        self.assertEqual(config.parse_config(with_coordinator()).coordinator.max_merges_per_day,
                         10)
        self.assertEqual(parsed(3).max_merges_per_day, 3)
        self.assertEqual(parsed(0).max_merges_per_day, 0)
        for cap in (11, -1, True, '3', 2.5):
            with self.subTest(cap=cap), self.assertRaisesRegex(ConfigError,
                                                               'max_merges_per_day'):
                parsed(cap)
        overlay = config.apply_overlay(ssdlc_project.data(), {
            'roles': {'coordinator': dict(TABLE, max_merges_per_day=2)},
            'apps': {'coordinator': {'app_id': 7777777,
                                     'slug': 'brushtyler-devloop-coordinator'}}})
        self.assertEqual(config.parse_config(overlay).coordinator.max_merges_per_day, 2)


class ReportTests(unittest.TestCase):
    """Criterion 2: the daily summary (#316) lists the reserved merges of the coordinator apart
    from the delegated ones."""

    def test_the_summary_lists_the_merges_of_the_coordinator(self):
        from test_devloop_report import CFG as RCFG, DISPATCHER, ReportGH, TRUST
        gh = ReportGH()
        day = '2026-11-22'

        def supervision_record(number, at, **record):
            gh.add(number, DISPATCHER, markdown.render_supervision(
                dict({'comment': 1}, **record), 'testo'), at)

        supervision_record(100, f'{day}T09:00:00Z', command='merge', by=COORDINATOR,
                           role='coordinator', outcome='merged',
                           reason=f'{SHA1} La regola di fiducia, approvata.', head=SHA1,
                           daily=1, daily_cap=10, daily_left=9, approval=APPROVAL)
        supervision_record(101, f'{day}T10:00:00Z', command='merge', by=COORDINATOR,
                           role='coordinator', outcome='refused', reason=f'{SHA2} altro')
        supervision_record(102, f'{day}T11:00:00Z', command='merge', by=SUPERVISOR_LOGIN,
                           outcome='merged', reason=f'{SHA1} delega', head=SHA1, daily=1,
                           daily_cap=5, daily_left=4)
        when = report.parse_day(day)
        s = report.Records(gh, TRUST, when, when, RCFG.agents, 'Italian').summary(when)
        self.assertEqual(s.coordinator, [
            ('09:00', 100, 'merge', COORDINATOR, f'{SHA1} La regola di fiducia, approvata.',
             'mergiata', 'giorno: 1 di 10'),
            ('10:00', 101, 'merge', COORDINATOR, f'{SHA2} altro', 'rifiutato', '—')])
        self.assertEqual([row[1] for row in s.delegated], [102])
        text = report.render_text(s)
        self.assertIn('### Merge riservati del coordinatore', text)
        self.assertIn('### Merge delegati al supervisore', text)
        self.assertIn(f'| 09:00 | #100 | `merge` | `{COORDINATOR}` | '
                      f'{SHA1} La regola di fiducia, approvata. | mergiata | giorno: 1 di 10 |',
                      text)
        self.assertFalse(report.Summary(day, coordinator=[s.coordinator[0]]).empty())


class PerimeterTests(unittest.TestCase):
    def test_merge_is_in_the_perimeter_of_the_coordinator(self):
        self.assertIn('merge', supervision.COORDINATOR_DELEGATED)
        self.assertIsNone(supervision.coordinator_refusal('merge'))
        self.assertNotIn('merge-budget', supervision.COORDINATOR_DELEGATED)

    def test_the_merge_is_documented(self):
        from devloop.context import PROMPT_DIR
        root = PROMPT_DIR.parents[1]

        def text(*parts):
            return ' '.join(root.joinpath(*parts).read_text(encoding='utf-8').split())
        guide = text('docs', 'devloop', 'guide.md')
        self.assertIn('#### The reserved merge of the coordinator', guide)
        reference = text('docs', 'devloop', 'reference.md')
        self.assertIn('`max_merges_per_day`', reference)
        self.assertIn('coordinator_merge', text('docs', 'devloop', 'protocol.md'))


if __name__ == '__main__':
    unittest.main()
