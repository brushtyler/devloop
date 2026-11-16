"""devloop: the supervisor queues issues and sets priorities with its own identity (#178)."""

import dataclasses
import unittest
from types import SimpleNamespace

from devloop.dispatcher import Dispatcher, UntrustedChange, label_admitters, plan
from devloop.github import GitHub
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
    """``issue_history`` with a chosen author, ready labeller and editor."""

    def __init__(self, author, labeler, edits=None):
        super().__init__()
        self.author, self.labeler, self.edits = author, labeler, edits

    def issue_history(self, number):
        return dict(super().issue_history(number), author=self.author,
                    labeled=[(READY, self.labeler, at(13))])


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
