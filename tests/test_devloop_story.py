"""devloop: the story identity and the dispatcher's story marker (#363, ADR 0004, P2-3a)."""

import ast
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import devloop
from devloop import ledger, markdown
from devloop.config import ConfigError, parse_config
from devloop.dispatcher import Dispatcher
from devloop.journal import JOURNAL, SNAPSHOT, JournalError, TurnJournal
from devloop.ledger import Ledger
from helpers import ssdlc_project
from test_devloop import CFG, implemented
from test_devloop_ledger import LedgerGH, ReportingRunner, TreeWS

STORY = dataclasses.replace(CFG, story_identity=True)
DISPATCHER = 'devloop-dispatcher[bot]'


class StoryGH(LedgerGH):
    """LedgerGH whose issues have the title and body of ``texts`` (``{number: (title, body)}``),
    edited by a trusted user when changed, and whose comments the test may add with an author."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.texts = {}

    def _text(self, number):
        return self.texts.get(number, (f'Issue {number}', 'Do the thing.'))

    def issue(self, number):
        title, body = self._text(number)
        return dict(super().issue(number), title=title, body=body)

    def issue_history(self, number):
        title, body = self._text(number)
        edited = (('2026-11-18T15:40:00Z', 'brushtyler') if number in self.texts
                  else (None, None))
        return dict(super().issue_history(number), title=title, body=body,
                    last_edited_at=edited[0], editor=edited[1])

    def add(self, number, login, body):
        self.next_id += 1
        self.thread.setdefault(number, []).append(
            {'id': self.next_id, 'author': {'login': login}, 'body': body})


def journal_events(state_dir):
    return [json.loads(line) for line in (Path(state_dir) / JOURNAL).read_text().splitlines()]


def story_markers(comments):
    return [m for m in (markdown.read_marker(c['body'], 'story') for c in comments)
            if m is not None]


def forged(story, root, issue):
    """A ``story`` marker of a child, valid in form, that someone other than the dispatcher
    publishes."""
    return markdown.publishable('Questa issue è figlia della storia.', 'story', {
        'story': story, 'repo': CFG.repo, 'root': root, 'issue': issue})


class StoryDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def make(self, cfg=STORY, *outputs):
        self.gh = StoryGH()
        self.logs = []
        runner = ReportingRunner(*(outputs or (implemented(), implemented(), implemented())),
                                 gh=self.gh)
        self.ledger = Ledger(self.gh, state_dir=self.state)
        return Dispatcher(cfg, self.gh, TreeWS(), runner, log=self.logs.append,
                          ledger=self.ledger)

    def test_the_story_is_recorded_in_the_journal_and_in_the_root_marker(self):
        d = self.make()
        d._do_implement(7)
        (request,) = self.gh.records(7, 'request')
        (event,) = [e for e in journal_events(self.state) if e['event'] == 'story']
        self.assertRegex(event['data']['story'], r'^story-[1-9][0-9]*-[0-9a-f]{8}$')
        self.assertEqual((event['data']['repo'], event['data']['issue'], event['data']['hash']),
                         (CFG.repo, 7, request['hash']))
        # The marker of the root, in the comment of its request record, signed by the dispatcher.
        (comment,) = [c for c in self.gh.thread[7]
                      if markdown.read_marker(c['body'], 'ledger')['type'] == 'request']
        self.assertEqual(markdown.read_marker(comment['body'], 'story'), {
            'story': event['data']['story'], 'repo': CFG.repo, 'root': 7, 'issue': 7,
            'hashes': [request['hash']]})
        self.assertIn('### Storia', comment['body'])
        self.assertTrue(comment['body'].startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        # Read again by a new process: the same story, and the marker confirmed by the journal.
        stories = TurnJournal(self.state).stories()
        self.assertEqual(list(stories), [event['data']['story']])
        self.assertEqual(self.ledger.story_of(7, self.gh.thread[7], CFG.repo)['story'],
                         event['data']['story'])

    def test_a_trusted_edit_adds_a_hash_and_keeps_the_identity(self):
        d = self.make()
        d._do_implement(7)
        # A trusted user changes the issue, and it is taken over again: a new request record.
        self.gh.texts[7] = ('Issue 7', 'Do the thing, better.')
        d._do_implement(7)
        # The same text again adds nothing.
        d._do_implement(7)
        first, second, third = self.gh.records(7, 'request')
        self.assertNotEqual(first['hash'], second['hash'])
        self.assertEqual(second['hash'], ledger.request_hash('Issue 7', 'Do the thing, better.'))
        self.assertEqual(third['hash'], second['hash'])
        events = [e for e in journal_events(self.state) if e['event'] in ('story', 'request')]
        self.assertEqual([e['event'] for e in events], ['story', 'request'])
        story = events[0]['data']['story']
        self.assertEqual(events[1]['data'], dict(events[1]['data'], story=story,
                                                 hash=second['hash']))
        (only,) = TurnJournal(self.state).stories().values()
        self.assertEqual((only['story'], only['repo'], only['issue'], only['hashes']),
                         (story, CFG.repo, 7, [first['hash'], second['hash']]))
        # Each request comment carries the story as it was then: the earlier hash is not lost.
        marks = story_markers(self.gh.thread[7])
        self.assertEqual([m['story'] for m in marks], [story] * 3)
        self.assertEqual([m['hashes'] for m in marks],
                         [[first['hash']], [first['hash'], second['hash']],
                          [first['hash'], second['hash']]])

    def test_a_journal_restored_from_before_the_root_marker_is_an_error(self):
        # Requests A, B and C published as usual; then the journal and its snapshot are
        # restored as they were after A, and a new process takes the root over with C.
        d = self.make()
        d._do_implement(7)
        saved = {name: (self.state / name).read_bytes() for name in (JOURNAL, SNAPSHOT)}
        for body in ('Do B.', 'Do C.'):
            self.gh.texts[7] = ('Issue 7', body)
            d._do_implement(7)
        a, b, c = (r['hash'] for r in self.gh.records(7, 'request'))
        self.assertEqual(story_markers(self.gh.thread[7])[-1]['hashes'], [a, b, c])
        for name, data in saved.items():
            (self.state / name).write_bytes(data)
        restarted = Ledger(self.gh, state_dir=self.state)
        d = Dispatcher(STORY, self.gh, TreeWS(), ReportingRunner(implemented(), gh=self.gh),
                       log=self.logs.append, ledger=restarted)
        requests, before = len(self.gh.records(7, 'request')), journal_events(self.state)
        # The marker goes beyond the journal: it contradicts it, and B is not dropped silently.
        with self.assertRaisesRegex(JournalError, 'not a prefix'):
            d._do_implement(7)
        self.assertEqual(journal_events(self.state), before)
        (only,) = TurnJournal(self.state).stories().values()
        self.assertEqual(only['hashes'], [a])
        self.assertEqual(len(self.gh.records(7, 'request')), requests)

    def test_a_child_without_the_dispatchers_marker_does_not_inherit_the_story(self):
        d = self.make()
        d._do_implement(7)
        root = self.ledger.journal.story_of(CFG.repo, 7)[0]['story']
        # Issue 8 declares itself a child of the story in its text, and an untrusted user
        # publishes a well-formed story marker of a child for it.
        self.gh.texts[8] = ('Issue 8', f'Figlia della storia `{root}` di #7.\n\n'
                            + markdown.marker('story', {'story': root, 'repo': CFG.repo,
                                                        'root': 7, 'issue': 8}))
        self.gh.add(8, 'stranger', forged(root, 7, 8))
        d._do_implement(8)
        story, role = self.ledger.journal.story_of(CFG.repo, 8)
        self.assertEqual((role, story['issue']), ('root', 8))
        self.assertNotEqual(story['story'], root)
        self.assertEqual(self.ledger.journal.stories()[root]['children'], [])

    def test_a_child_inherits_the_story_from_the_dispatchers_marker_alone(self):
        d = self.make()
        d._do_implement(7)
        root = self.ledger.journal.story_of(CFG.repo, 7)[0]['story']
        self.ledger.mark_child(8, root, CFG.repo)
        before = journal_events(self.state)
        d._do_implement(8)
        # No story of its own and nothing new about stories: it is the root's child.
        self.assertEqual([e for e in journal_events(self.state)[len(before):]
                          if e['event'] in ('story', 'request', 'child')], [])
        self.assertEqual(self.ledger.journal.story_of(CFG.repo, 8)[1], 'child')
        self.assertIn(f'  #8 is a child of story {root} (root #7)', self.logs)
        self.assertEqual(len(story_markers(self.gh.thread[8])), 1)  # the child's marker only

    def test_existing_issues_without_marker_work_as_today(self):
        # Off by default: the takeover writes the request record as before, no story.
        self.assertFalse(ssdlc_project.load().story_identity)
        d = self.make(CFG)
        d._do_implement(7)
        (comment,) = [c for c in self.gh.thread[7]
                      if markdown.read_marker(c['body'], 'ledger')['type'] == 'request']
        self.assertIsNone(markdown.read_marker(comment['body'], 'story'))
        self.assertEqual(comment['body'], ledger.render(
            markdown.read_marker(comment['body'], 'ledger'), language=CFG.language))
        self.assertFalse([e for e in journal_events(self.state)
                          if e['event'] in ('story', 'request', 'child')])
        self.assertEqual(self.ledger.journal.stories(), {})
        off = (self.gh.labels[7], len(self.gh.created))
        self.assertEqual(off[1], 1)  # the PR is opened
        # On, an issue without a marker is no child: it is read as having no story, and its
        # takeover makes it the root of a story of its own; the step goes on as with it off.
        self.assertIsNone(self.ledger.story_of(9, self.gh.issue_comments(9), CFG.repo))
        d = self.make(STORY)
        d._do_implement(9)
        self.assertEqual(self.ledger.journal.story_of(CFG.repo, 9)[1], 'root')
        self.assertEqual((self.gh.labels[9], len(self.gh.created)), off)


class StoryLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gh = StoryGH()
        self.ledger = Ledger(self.gh, state_dir=self.tmp.name)
        self.ledger.signed = lambda login: login == DISPATCHER
        self.root = self.ledger.journal.story(CFG.repo, 7, 'h1')['story']

    def test_only_the_dispatchers_marker_counts(self):
        # The operator and an untrusted user publish valid markers of a child: not inherited.
        for login in ('brushtyler', 'stranger'):
            self.gh.add(8, login, forged(self.root, 7, 8))
        self.assertIsNone(self.ledger.story_of(8, self.gh.issue_comments(8), CFG.repo))
        # Without a check of the dispatcher's signature, no marker counts.
        self.gh.add(8, DISPATCHER, forged(self.root, 7, 8))
        self.ledger.signed = None
        self.assertIsNone(self.ledger.story_of(8, self.gh.issue_comments(8), CFG.repo))

    def test_a_marker_the_journal_does_not_confirm_is_an_error(self):
        # Signed by the dispatcher, but the journal has no such child: the journal decides.
        self.gh.add(8, DISPATCHER, forged(self.root, 7, 8))
        with self.assertRaisesRegex(JournalError, 'not confirmed by the journal'):
            self.ledger.story_of(8, self.gh.issue_comments(8), CFG.repo)
        # A story the journal does not hold, or the marker of another issue.
        for story, issue in (('story-99-0123abcd', 9), (self.root, 10)):
            with self.subTest(story=story, issue=issue):
                self.gh.add(9, DISPATCHER, forged(story, 7, issue))
                with self.assertRaises(JournalError):
                    self.ledger.story_of(9, self.gh.issue_comments(9), CFG.repo)

    def test_the_hashes_of_a_root_marker_must_be_a_prefix_of_the_journals(self):
        self.ledger.journal.story(CFG.repo, 7, 'h2')

        def root(hashes):
            return markdown.publishable('Radice.', 'story', {
                'story': self.root, 'repo': CFG.repo, 'root': 7, 'issue': 7, 'hashes': hashes})
        # Equal to the journal, or behind it (a marker whose write failed): valid.
        for hashes in (['h1', 'h2'], ['h1']):
            with self.subTest(hashes=hashes):
                comments = [{'author': {'login': DISPATCHER}, 'body': root(hashes)}]
                self.assertEqual(self.ledger.story_of(7, comments, CFG.repo)['hashes'],
                                 ['h1', 'h2'])
        # Diverging from it, or beyond it (a journal restored from before): an integrity error.
        for hashes in (['h2'], ['h1', 'h3'], ['h2', 'h1'], ['h1', 'h2', 'h3']):
            with self.subTest(hashes=hashes):
                comments = [{'author': {'login': DISPATCHER}, 'body': root(hashes)}]
                with self.assertRaisesRegex(JournalError, 'not a prefix'):
                    self.ledger.story_of(7, comments, CFG.repo)

    def test_the_child_marker_writer(self):
        comment = self.ledger.mark_child(8, self.root, CFG.repo)
        (written,) = self.gh.thread[8]
        self.assertEqual(written['id'], comment)
        self.assertTrue(written['body'].startswith('> [!NOTE]\n> **Bot:** devloop dispatcher'))
        self.assertEqual(markdown.read_marker(written['body'], 'story'),
                         {'story': self.root, 'repo': CFG.repo, 'root': 7, 'issue': 8})
        self.assertEqual(self.ledger.journal.stories()[self.root]['children'], [8])
        # Written by the dispatcher's identity, it is the child's story.
        self.gh.thread[8][0]['author'] = {'login': DISPATCHER}
        self.assertEqual(self.ledger.story_of(8, self.gh.issue_comments(8), CFG.repo)['story'],
                         self.root)
        # Recorded once; refused for an unknown story, the root, another repository, or an
        # issue that holds another story already.
        self.ledger.journal.child(self.root, CFG.repo, 8)
        self.assertEqual(self.ledger.journal.stories()[self.root]['children'], [8])
        other = self.ledger.journal.story(CFG.repo, 20, 'h2')['story']
        for story, repo, number in (('story-99-0123abcd', CFG.repo, 9), (self.root, CFG.repo, 7),
                                    (self.root, 'other/repo', 9), (other, CFG.repo, 8),
                                    (self.root, CFG.repo, 20)):
            with self.subTest(story=story, repo=repo, number=number):
                with self.assertRaises(JournalError):
                    self.ledger.mark_child(number, story, repo)
        self.assertEqual(len(self.gh.thread[8]), 1)
        # A child never becomes the root of a story.
        with self.assertRaisesRegex(JournalError, 'child of story'):
            self.ledger.journal.story(CFG.repo, 8, 'h3')

    def test_no_flow_calls_the_child_marker_writer(self):
        # Which act authorizes the association of a child is decided with step 3 (#363): until
        # then nothing in devloop calls it.
        # ``mark_child`` is referenced nowhere in the package (by attribute, name or string,
        # e.g. ``getattr``), and ``TurnJournal.child`` only inside ``Ledger.mark_child``.
        package = Path(devloop.__file__).parent
        uses, child_calls = [], []
        for path in sorted(package.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            writer = [n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == 'mark_child']
            inside = {id(n) for w in writer for n in ast.walk(w)}
            for node in ast.walk(tree):
                where = f'{path.relative_to(package)}:{getattr(node, "lineno", 0)}'
                if (isinstance(node, ast.Attribute) and node.attr == 'mark_child'
                        or isinstance(node, ast.Name) and node.id == 'mark_child'
                        or isinstance(node, ast.Constant) and node.value == 'mark_child'):
                    uses.append(where)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'child' and id(node) not in inside):
                    child_calls.append(where)
        self.assertEqual(uses, [])
        self.assertEqual(child_calls, [])
        self.assertTrue(callable(Ledger.mark_child))


class StoryRecordTests(unittest.TestCase):
    def test_the_journal_keeps_the_stories_across_processes_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = TurnJournal(tmp)
            story = j.story(CFG.repo, 7, 'h1')
            self.assertEqual(j.story(CFG.repo, 7, 'h1'), story)  # the same hash: nothing new
            j.story(CFG.repo, 7, 'h2')
            j.child(story['story'], CFG.repo, 8)
            again = TurnJournal(tmp).stories()[story['story']]
            self.assertEqual((again['hashes'], again['children']), (['h1', 'h2'], [8]))
            size = (Path(tmp) / JOURNAL).stat().st_size
            reader = TurnJournal(tmp, read_only=True)
            reader.story(CFG.repo, 7, 'h3')
            self.assertEqual((Path(tmp) / JOURNAL).stat().st_size, size)

    def test_the_story_marker_structure(self):
        root = {'story': 'story-1-0123abcd', 'repo': CFG.repo, 'root': 7, 'issue': 7,
                'hashes': ['h1']}
        child = {'story': 'story-1-0123abcd', 'repo': CFG.repo, 'root': 7, 'issue': 8}
        for mark in (root, child):
            self.assertEqual(markdown.read_marker(markdown.publishable('x', 'story', mark),
                                                  'story'), mark)
        for bad in (dict(root, hashes=[]), dict(root, hashes=None), dict(child, hashes=['h1']),
                    dict(root, story='turn-1-0123abcd'), dict(root, root=0),
                    dict(child, repo=''), dict(child, issue='8')):
            with self.subTest(bad=bad):
                self.assertIsNone(markdown.read_marker(markdown.publishable('x', 'story', bad),
                                                       'story'))

    def test_the_configuration_key(self):
        data = ssdlc_project.data()
        data.pop('story_identity', None)
        self.assertFalse(parse_config(data).story_identity)
        self.assertTrue(parse_config(dict(data, story_identity=True)).story_identity)
        with self.assertRaises(ConfigError):
            parse_config(dict(data, story_identity='yes'))


if __name__ == '__main__':
    unittest.main()
