"""devloop: the supervisor queues issues and sets priorities with its own identity (#178)."""

import dataclasses
import json
import unittest
from types import SimpleNamespace

import doc_contracts as docs
from devloop.dispatch_actions import ready_approval
from devloop.dispatch_supervision import UntrustedChange
from devloop.dispatcher import Dispatcher, label_admitters, untrusted_change
from devloop.github import _ISSUE_HISTORY, GitHub, graphql_login
from devloop.planning import plan
from test_devloop import CFG, FakeGH, FakeIdentities, FakeRunner, FakeWS, implemented

READY, HIGH, LOW = 'agent:ready', 'priority:high', 'priority:low'
SUPERVISOR = 'agy'
SUPERVISOR_LOGIN = FakeIdentities.LOGINS[SUPERVISOR]
DEVLOOP_AUTHOR = FakeIdentities.LOGINS['codex']
# Room for every ready issue: trust and order are the only things that decide.
BASE = dataclasses.replace(CFG, max_open_prs=10, max_actions_per_tick=10)
SCFG = dataclasses.replace(BASE, supervisor=SUPERVISOR)


def at(day):
    return f'2026-11-{day:02d}T10:00:00Z'


def added(label, day, by=SUPERVISOR_LOGIN):
    return {'label': label, 'by': by, 'at': at(day)}


class QueueGH(FakeGH):
    """Ready issues as ``{number: (author, labels, events)}``, events oldest first. The raw
    author is in the form of ``gh issue list``: an app is ``app/<slug>`` with ``is_bot``."""

    def __init__(self, issues):
        super().__init__()
        self.queue = issues

    def issues_with_label(self, label):
        out = []
        for n, (author, labels, _) in sorted(self.queue.items()):
            if label not in labels:
                continue
            raw = ({'login': 'app/' + author[:-len('[bot]')], 'is_bot': True}
                   if author.endswith('[bot]') else {'login': author, 'is_bot': False})
            out.append({'number': n, 'title': f'Issue {n}', 'author': raw,
                        'labels': [{'name': name} for name in sorted(labels)]})
        return out

    def labeled_events(self, number):
        return [dict(event) for event in self.queue[number][2]]


class HistoryGH(FakeGH):
    """``issue_history`` with a chosen author, ready labeller and editor; ``labeled``, when
    given, replaces the single ready label."""

    def __init__(self, author, labeler, edits=None, labeled=None):
        super().__init__()
        self.author, self.labeler, self.edits = author, labeler, edits
        self.labeled = labeled or [(READY, labeler, at(13))]

    def issue_history(self, number):
        return dict(super().issue_history(number), author=self.author,
                    labeled=list(self.labeled))


class Taken(Exception):
    """Stops ``_do_implement`` at the ledger's request snapshot."""


class RequestLedger:
    """Records the request snapshot of the takeover, then stops the implementation."""

    def __init__(self):
        self.requests = []

    def request(self, number, **fields):
        self.requests.append(dict(fields, number=number))
        raise Taken


class QueueTests(unittest.TestCase):
    def make(self, issues, cfg=SCFG, identities=True):
        self.gh = QueueGH(issues)
        ids = FakeIdentities(self.gh) if identities else None
        return Dispatcher(cfg, self.gh, FakeWS(), FakeRunner(), log=lambda m: None,
                          identities=ids)

    def started(self, d, cfg=SCFG):
        return [a.number for a in plan(d.snapshot(), cfg) if a.kind == 'implement']

    def test_supervisor_ready_label_starts_only_issues_of_trusted_authors(self):
        d = self.make({
            1: ('brushtyler', {READY}, [added(READY, 1)]),
            # The same label by the supervisor on a stranger's issue does not admit it.
            2: ('stranger', {READY}, [added(READY, 2)]),
            # An issue opened by one of devloop's apps can be queued by the supervisor.
            3: (DEVLOOP_AUTHOR, {READY}, [added(READY, 3)]),
            # On a stranger's issue a trusted user's label still counts, as before.
            4: ('stranger', {READY}, [added(READY, 4, by='brushtyler')]),
            # The latest agent:ready decides: a trusted label replaced by the supervisor's on a
            # stranger's issue no longer admits it.
            5: ('stranger', {READY}, [added(READY, 1, by='brushtyler'), added(READY, 5)]),
        })
        snap = d.snapshot()
        self.assertEqual({i['number']: i['admitted'] for i in snap.ready_issues},
                         {1: True, 2: False, 3: True, 4: True, 5: False})
        self.assertEqual(self.started(d), [1, 3, 4])

    def test_supervisor_priority_reorders_like_a_trusted_user(self):
        issues = {
            1: ('brushtyler', {READY}, [added(READY, 2, by='brushtyler')]),
            2: ('brushtyler', {READY, HIGH}, [added(READY, 3, by='brushtyler'), added(HIGH, 3)]),
            3: ('brushtyler', {READY, LOW}, [added(READY, 1, by='brushtyler'), added(LOW, 3)]),
        }
        # Without priorities the oldest agent:ready starts first.
        plain = {n: (author, {READY}, events[:1]) for n, (author, _, events) in issues.items()}
        self.assertEqual(self.started(self.make(plain)), [3, 1, 2])
        by_supervisor = self.started(self.make(issues))
        trusted = {n: (author, labels, [dict(e, by='brushtyler') for e in events])
                   for n, (author, labels, events) in issues.items()}
        self.assertEqual(by_supervisor, self.started(self.make(trusted)))
        self.assertEqual(by_supervisor, [2, 1, 3])
        # On a stranger's issue the supervisor's priority is ignored.
        d = self.make({
            1: ('brushtyler', {READY}, [added(READY, 1, by='brushtyler')]),
            2: ('stranger', {READY, HIGH}, [added(READY, 2, by='brushtyler'), added(HIGH, 2)]),
        })
        self.assertEqual({i['number']: i['priority'] for i in d.snapshot().ready_issues},
                         {1: 'normal', 2: 'normal'})
        self.assertEqual(self.started(d), [1, 2])

    def test_without_a_supervisor_only_trusted_users_count(self):
        issues = {
            1: ('brushtyler', {READY}, [added(READY, 1)]),
            2: ('brushtyler', {READY}, [added(READY, 2, by='brushtyler')]),
            3: ('brushtyler', {READY, HIGH}, [added(READY, 3, by='brushtyler'), added(HIGH, 3)]),
        }
        # No [roles].supervisor: the supervisor's app is just another identity.
        d = self.make(issues, cfg=BASE)
        snap = d.snapshot()
        self.assertEqual({i['number']: (i['admitted'], i['priority']) for i in snap.ready_issues},
                         {1: (False, 'normal'), 2: (True, 'normal'), 3: (True, 'normal')})
        self.assertEqual(self.started(d, BASE), [2, 3])
        # A supervisor without its own app acts with the operator's login: nothing new counts.
        d = self.make(issues, identities=False)
        self.assertEqual(self.started(d), [2, 3])


class IssueContextTests(unittest.TestCase):
    def make(self, gh, cfg=SCFG):
        self.gh = gh
        return Dispatcher(cfg, gh, FakeWS(), FakeRunner(implemented()), log=lambda m: None,
                          identities=FakeIdentities(gh))

    def test_supervisor_label_makes_a_trusted_authors_text_usable(self):
        issue, _ = self.make(HistoryGH('brushtyler', SUPERVISOR_LOGIN))._issue_context(7)
        self.assertEqual(issue['body'], 'Do the thing.')
        issue, _ = self.make(HistoryGH(DEVLOOP_AUTHOR, SUPERVISOR_LOGIN))._issue_context(7)
        self.assertEqual(issue['body'], 'Do the thing.')

    def test_supervisor_label_on_a_strangers_issue_is_refused(self):
        with self.assertRaisesRegex(UntrustedChange, 'nessuna label'):
            self.make(HistoryGH('stranger', SUPERVISOR_LOGIN))._issue_context(7)
        # The same holds without a supervisor configured, even on a trusted author's issue.
        with self.assertRaisesRegex(UntrustedChange, 'nessuna label'):
            self.make(HistoryGH('brushtyler', SUPERVISOR_LOGIN), BASE)._issue_context(7)

    def test_an_edit_by_the_supervisor_after_the_label_voids_trust(self):
        gh = HistoryGH('brushtyler', SUPERVISOR_LOGIN, edits=(at(14), SUPERVISOR_LOGIN))
        with self.assertRaisesRegex(UntrustedChange, 'modificato da'):
            self.make(gh)._issue_context(7)

    def test_implement_starts_on_the_supervisors_label(self):
        d = self.make(HistoryGH('brushtyler', SUPERVISOR_LOGIN))
        d._do_implement(7)
        self.assertEqual(len(self.gh.created), 1)

    def test_a_trusted_label_replaced_by_the_supervisor_after_planning_is_refused(self):
        # A stranger's issue approved by a trusted user, then relabelled by the supervisor
        # between the snapshot and the implementation: the latest agent:ready decides.
        replaced = [(READY, 'brushtyler', at(13)), (READY, SUPERVISOR_LOGIN, at(14))]
        gh = HistoryGH('stranger', None, labeled=replaced)
        d = self.make(gh)
        with self.assertRaisesRegex(UntrustedChange, 'nessuna label'):
            d._issue_context(7)
        with self.assertRaisesRegex(UntrustedChange, 'nessuna label'):
            d._do_implement(7)
        self.assertEqual(gh.created, [])
        # The same replacement on a trusted author's issue still admits it.
        issue, _ = self.make(HistoryGH('brushtyler', None, labeled=replaced))._issue_context(7)
        self.assertEqual(issue['body'], 'Do the thing.')

    def test_the_ledger_records_the_latest_ready_label(self):
        gh = HistoryGH('brushtyler', None, labeled=[(READY, 'brushtyler', at(13)),
                                                    (READY, SUPERVISOR_LOGIN, at(14))])
        d = self.make(gh)
        d.ledger = RequestLedger()
        with self.assertRaises(Taken):
            d._do_implement(7)
        (request,) = d.ledger.requests
        self.assertEqual(request['labeled'], (SUPERVISOR_LOGIN, at(14)))


class UntrustedChangeTests(unittest.TestCase):
    def check(self, labeled, admitters):
        history = {'title': 't', 'body': 'b', 'author': 'stranger', 'last_edited_at': None,
                   'editor': None, 'renamed': [], 'labeled': labeled}
        return untrusted_change(history, READY, ['brushtyler'], admitters, 'Italian')

    def test_the_latest_ready_label_decides(self):
        trusted, supervisor = (READY, 'brushtyler', at(13)), (READY, SUPERVISOR_LOGIN, at(14))
        self.assertIsNone(self.check([trusted], {'brushtyler'}))
        self.assertIn('nessuna label', self.check([trusted, supervisor], {'brushtyler'}))
        self.assertIsNone(self.check([trusted, supervisor], {'brushtyler', SUPERVISOR_LOGIN}))
        # Other labels in between do not matter.
        self.assertIsNone(self.check([trusted, (HIGH, SUPERVISOR_LOGIN, at(14))], {'brushtyler'}))
        self.assertEqual(ready_approval([trusted, supervisor], READY, {'brushtyler'}), None)
        self.assertEqual(ready_approval([supervisor, trusted], READY, {'brushtyler'}),
                         ('brushtyler', at(13)))
        self.assertIsNone(ready_approval([], READY, {'brushtyler'}))


class IssueHistoryTests(unittest.TestCase):
    """``GitHub.issue_history`` reads GraphQL, where an app's login is a bare ``<slug>``."""

    def history(self, payload):
        def fake_run(argv, **kw):
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')
        return GitHub('o/r', run=fake_run).issue_history(7)

    @staticmethod
    def bot(slug):
        return {'__typename': 'Bot', 'login': slug}

    def test_app_logins_are_in_rest_form(self):
        slug = SUPERVISOR_LOGIN[:-len('[bot]')]
        author = DEVLOOP_AUTHOR[:-len('[bot]')]
        payload = {'data': {'repository': {'issue': {
            'title': 'T', 'body': 'B', 'author': self.bot(author), 'lastEditedAt': at(14),
            'editor': self.bot(slug), 'timelineItems': {'nodes': [
                {'__typename': 'LabeledEvent', 'createdAt': at(13), 'actor': self.bot(slug),
                 'label': {'name': READY}},
                {'__typename': 'LabeledEvent', 'createdAt': at(13),
                 'actor': {'__typename': 'User', 'login': 'brushtyler'},
                 'label': {'name': HIGH}},
                {'__typename': 'RenamedTitleEvent', 'createdAt': at(15),
                 'actor': self.bot('brushtyler')},
                {'__typename': 'RenamedTitleEvent', 'createdAt': at(15), 'actor': None}]}}}}}
        history = self.history(payload)
        self.assertEqual(history['author'], DEVLOOP_AUTHOR)
        self.assertEqual(history['editor'], SUPERVISOR_LOGIN)
        self.assertEqual(history['labeled'], [(READY, SUPERVISOR_LOGIN, at(13)),
                                              (HIGH, 'brushtyler', at(13))])
        # An app whose slug is a trusted user's login is not mistaken for that user.
        self.assertEqual(history['renamed'], [('brushtyler[bot]', at(15)), (None, at(15))])
        # The queried fields include each actor's type.
        self.assertEqual(graphql_login({'__typename': 'Bot', 'login': 'x[bot]'}), 'x[bot]')
        self.assertEqual(graphql_login({'__typename': 'User', 'login': 'x'}), 'x')
        self.assertIsNone(graphql_login(None))
        self.assertEqual(_ISSUE_HISTORY.count('__typename login'), 4)

    def test_the_supervisors_app_label_starts_a_trusted_authors_issue(self):
        # End to end from the GraphQL read: the app's bare slug becomes its REST login and the
        # supervisor's label admits an issue opened by one of devloop's apps.
        slug = SUPERVISOR_LOGIN[:-len('[bot]')]
        payload = {'data': {'repository': {'issue': {
            'title': 'Issue 7', 'body': 'Do the thing.',
            'author': self.bot(DEVLOOP_AUTHOR[:-len('[bot]')]), 'lastEditedAt': None,
            'editor': None, 'timelineItems': {'nodes': [
                {'__typename': 'LabeledEvent', 'createdAt': at(13), 'actor': self.bot(slug),
                 'label': {'name': READY}}]}}}}}
        history = self.history(payload)
        gh = HistoryGH(history['author'], None, labeled=history['labeled'])
        d = Dispatcher(SCFG, gh, FakeWS(), FakeRunner(), log=lambda m: None,
                       identities=FakeIdentities(gh))
        issue, _ = d._issue_context(7)
        self.assertEqual(issue['admitters'], {'brushtyler', SUPERVISOR_LOGIN})


class DocumentationTests(unittest.TestCase):
    def test_the_rule_is_documented(self):
        section = docs.plan_section('docs/devloop/guide.md', 'Labels from the supervisor',
                                    root=docs.ROOT)
        docs.assert_documented(self, section, [
            '[roles].supervisor', 'agent:ready', 'priority:*', 'priority_labels',
            "only on issues opened by a user in trusted_users or by one of devloop's apps",
            "on an issue opened by anyone else only a trusted user's label counts",
            'the latest labeled event of the label decides',
            'changes the start order exactly as a trusted user\'s would',
            'without [roles].supervisor', 'only trusted_users count, as before'], issues=(178,))
        # The page is reachable from the README, and the start step links to the section.
        self.assertIn('docs/devloop/guide.md', docs.user_facing_docs().names)
        page = 'devloop: operator guide'
        docs.assert_documented(self, docs.plan_section('docs/devloop/guide.md', page,
                                                       root=docs.ROOT),
                               ['(#labels-from-the-supervisor)'])


class LabelAdmittersTests(unittest.TestCase):
    def test_label_admitters(self):
        trusted, devloop = ('brushtyler',), {DEVLOOP_AUTHOR}
        self.assertEqual(label_admitters('brushtyler', trusted, SUPERVISOR_LOGIN, devloop),
                         {'brushtyler', SUPERVISOR_LOGIN})
        self.assertEqual(label_admitters(DEVLOOP_AUTHOR, trusted, SUPERVISOR_LOGIN, devloop),
                         {'brushtyler', SUPERVISOR_LOGIN})
        self.assertEqual(label_admitters('stranger', trusted, SUPERVISOR_LOGIN, devloop),
                         {'brushtyler'})
        self.assertEqual(label_admitters(None, trusted, SUPERVISOR_LOGIN, devloop), {'brushtyler'})
        self.assertEqual(label_admitters('brushtyler', trusted, None, devloop), {'brushtyler'})

    def test_ready_issues_are_read_with_their_author(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout='[]', stderr='')

        GitHub('o/r', run=fake_run).issues_with_label(READY)
        (argv,) = calls
        self.assertIn('author', argv[argv.index('--json') + 1].split(','))


if __name__ == '__main__':
    unittest.main()
