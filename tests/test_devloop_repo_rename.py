"""devloop: the rename of the repository (#540, ADR 0007, R3).

``repo_aliases``, a trust key of the operator in the overlay, empty by default, lists the names
the repository had before a rename. A link to a comment under one of them counts only when the
dispatcher finds the comment by id in the configured repository, with the API, and its author is
in ``trusted_users``: a comment that is not there (a 404) or of another author is refused, and
without ``repo_aliases`` the old name is another repository. The records of the journal and the
markers written under the old name stay the repository's.
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import config, markdown, supervision
from devloop.config import ConfigError
from devloop.dispatcher import Dispatcher
from devloop.github import GitHubError
from devloop.journal import JOURNAL, SNAPSHOT, JournalError, TurnJournal
from devloop.ledger import Ledger
from helpers import ssdlc_project
from test_devloop import CFG, FakeRunner, FakeWS
from test_devloop_delegated_merge import (ADR, DECISIONS, LINK, ON, PRECISION, STRANGER_COMMENT,
                                          TRUSTED_COMMENT, Case, adr_entry)
from test_devloop_dependencies import DCFG, DependencyGH, block, ready
from test_devloop_story import DISPATCHER, StoryGH

OLD = CFG.repo  # the name of ssdlc before ADR 0007
NEW = 'brushtyler/devloop'
assert OLD != NEW


def renamed(cfg, aliases=(OLD,)):
    """``cfg`` after the rename: ``repo`` the new name, ``repo_aliases`` the old ones."""
    return dataclasses.replace(cfg, repo=NEW, repo_aliases=tuple(aliases))


class ConfigTests(unittest.TestCase):
    def load(self, **overlay):
        return config.parse_config(config.apply_overlay(ssdlc_project.data(), overlay))

    def test_empty_by_default(self):
        self.assertEqual(ssdlc_project.load().repo_aliases, ())
        self.assertEqual(config.Config.__dataclass_fields__['repo_aliases'].default, ())

    def test_the_overlay_declares_the_former_names(self):
        cfg = self.load(repo=NEW, repo_aliases=[OLD])
        self.assertEqual((cfg.repo, cfg.repo_aliases), (NEW, (OLD,)))

    def test_a_malformed_alias_is_refused(self):
        for aliases, why in ((OLD, 'expected a list'), (['ssdlc'], 'owner/name'),
                             (['a/..'], 'owner/name'), ([NEW.upper()], 'is repo itself'),
                             ([OLD, OLD.upper()], 'listed twice')):
            with self.subTest(aliases=aliases), self.assertRaisesRegex(ConfigError, why):
                self.load(repo=NEW, repo_aliases=aliases)

    def test_a_trust_key_of_the_overlay_alone(self):
        self.assertIn('repo_aliases', config.TRUST_KEYS)
        self.assertNotIn('repo_aliases', config.LIVE_KEYS)
        with self.assertRaisesRegex(ConfigError, 'set it in the local overlay'):
            config.compose(ssdlc_project.data(), project={'repo_aliases': [OLD]})


class LinkTests(unittest.TestCase):
    def test_a_link_under_an_alias_is_a_link_of_the_repository(self):
        text = (f'https://github.com/{OLD}/issues/12#issuecomment-34 e '
                f'https://github.com/{NEW}/pull/5#issuecomment-6 e '
                'https://github.com/x/y/issues/1#issuecomment-2')
        self.assertEqual(supervision.comment_links(text, NEW, (OLD,)), [(12, 34), (5, 6)])
        # Without the alias the old name is another repository.
        self.assertEqual(supervision.comment_links(text, NEW), [(5, 6)])
        block = f'https://github.com/{OLD}/issues/1#issuecomment-2\n'
        problems, blocks = supervision.adr_problems([adr_entry('docs/adr/a.md', block)], NEW,
                                                    (OLD,))
        self.assertEqual((problems, blocks), ([], [('docs/adr/a.md', [(1, 2)])]))
        problems, blocks = supervision.adr_problems([adr_entry('docs/adr/a.md', block)], NEW)
        self.assertEqual(blocks, [])
        self.assertIn('non ha il link a un commento di questo repository', problems[0])


class DelegatedMergeTests(Case):
    """The links of the ADR blocks of a delegated merge (#486), the repository renamed: the
    comments of ``DecisionGH`` are those of the configured repository, read with the API."""

    RENAMED = renamed(ON)

    def setUp(self):
        self.assertEqual(LINK, f'https://github.com/{OLD}/issues/{DECISIONS}'
                               f'#issuecomment-{TRUSTED_COMMENT}')

    def merged(self, adr, cfg=RENAMED):
        d = self.make(cfg, adr=[adr_entry(ADR, adr)])
        self.merge()
        return d

    def test_a_link_under_the_old_name_to_a_trusted_comment_of_the_repository(self):
        d = self.merged(PRECISION)
        self.apply(d)
        self.assertEqual(self.decisions()[-1]['outcome'], 'merged')
        self.assertEqual(len(self.gh.merged), 1)

    def test_the_new_name_is_accepted_too(self):
        d = self.merged(PRECISION.replace(OLD, NEW))
        self.apply(d)
        self.assertEqual(len(self.gh.merged), 1)

    def test_a_comment_that_is_not_in_the_repository_is_refused(self):
        # The id of a comment of another repository that took the old name: not among the
        # comments of the issue in the configured repository.
        d = self.merged(PRECISION.replace(str(TRUSTED_COMMENT), '7054515999'))
        self.assert_refused(d, 'cita solo commenti che non sono di un utente fidato')

    def test_an_issue_the_api_does_not_find_is_refused(self):
        d = self.merged(PRECISION.replace(f'/issues/{DECISIONS}#', '/issues/9999#'))
        reads = []

        def missing(number, read=self.gh.issue_comments):
            reads.append(number)
            if number == 9999:
                raise GitHubError(f'gh api repos/{NEW}/issues/9999/comments: HTTP 404: Not Found')
            return read(number)
        self.gh.issue_comments = missing
        self.assert_refused(d, 'cita solo commenti che non sono di un utente fidato')
        self.assertIn(9999, reads)
        self.assertIn('  the comments of #9999 cannot be read: gh api repos/brushtyler/devloop/'
                      'issues/9999/comments: HTTP 404: Not Found', self.logs)

    def test_a_comment_of_another_author_is_refused(self):
        d = self.merged(PRECISION.replace(str(TRUSTED_COMMENT), str(STRANGER_COMMENT)))
        self.assert_refused(d, 'cita solo commenti che non sono di un utente fidato')

    def test_without_repo_aliases_the_old_name_is_refused(self):
        d = self.merged(PRECISION, renamed(ON, aliases=()))
        self.assert_refused(d, 'non ha il link a un commento di questo repository')


class DependencyTests(unittest.TestCase):
    def test_a_block_under_the_old_name_is_of_the_repository(self):
        blocks = {1: [block(2, repo=OLD)], 3: [block(2, repo=NEW)]}
        for cfg, seen in ((renamed(DCFG), '#2'), (renamed(DCFG, ()), f'{OLD}#2')):
            with self.subTest(aliases=cfg.repo_aliases):
                gh = DependencyGH(ready(1, 3), blocks)
                d = Dispatcher(cfg, gh, FakeWS(), FakeRunner(), log=lambda _: None)
                found = {i['number']: i['blocked_by'] for i in d.snapshot().ready_issues}
                self.assertEqual(found, {1: [seen], 3: ['#2']})


class RecordsTests(unittest.TestCase):
    """The journal and the markers written before the rename, under the old name, read after
    it with ``repo_aliases``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        # Before the rename: a story, its active attempt and a turn left without outcome.
        before = TurnJournal(self.state)
        self.story = before.story(OLD, 7, 'h1')['story']
        self.attempt = before.open_attempt(self.story, 7)['attempt']
        self.turn = before.intent(7, issue=7, repo=OLD, kind='implement', agent='claude',
                                  model='m', label='l')
        self.files = {name: (self.state / name).read_bytes() for name in (JOURNAL, SNAPSHOT)}

    def after(self, aliases={OLD: NEW}):
        return TurnJournal(self.state, aliases=aliases)

    def test_the_journal_written_under_the_old_name_stays_valid(self):
        journal = self.after()
        story, role = journal.story_of(NEW, 7)
        self.assertEqual((story['story'], story['repo'], role), (self.story, NEW, 'root'))
        self.assertEqual(journal.active_attempt_of(NEW, 7)['attempt'], self.attempt)
        self.assertEqual(journal.attempts()[self.attempt]['repo'], NEW)
        self.assertEqual(journal.turns()[self.turn]['repo'], NEW)
        # A turn left open by the process of before still blocks the work, under the new name.
        self.assertEqual([t['turn'] for t in journal.blocking(NEW, 7, 7)], [self.turn])
        # Read, never rewritten.
        self.assertEqual({n: (self.state / n).read_bytes() for n in self.files}, self.files)
        # The same story goes on: a new request is added to it, and a new attempt waits for
        # the active one.
        self.assertEqual(journal.story(NEW, 7, 'h2')['story'], self.story)
        self.assertEqual(journal.stories()[self.story]['hashes'], ['h1', 'h2'])
        self.assertEqual(journal.child(self.story, NEW, 8)['children'], [8])
        self.assertEqual(len(journal.stories()), 1)

    def test_without_aliases_the_old_name_is_another_repository(self):
        journal = self.after(None)
        self.assertIsNone(journal.story_of(NEW, 7))
        self.assertIsNone(journal.active_attempt_of(NEW, 7))
        self.assertEqual(journal.blocking(NEW, 7, 7), [])

    def test_the_story_marker_written_under_the_old_name_stays_valid(self):
        gh = StoryGH()
        gh.add(7, DISPATCHER, markdown.publishable('Radice.', 'story', {
            'story': self.story, 'repo': OLD, 'root': 7, 'issue': 7, 'hashes': ['h1']}))
        ledger = Ledger(gh, state_dir=self.state, repo_aliases={OLD: NEW})
        ledger.signed = lambda login: login == DISPATCHER
        self.assertEqual(ledger.story_of(7, gh.issue_comments(7), NEW)['story'], self.story)
        # Without the alias the journal does not confirm it: the story is not used.
        plain = Ledger(gh, state_dir=self.state)
        plain.signed = ledger.signed
        with self.assertRaisesRegex(JournalError, 'not confirmed by the journal'):
            plain.story_of(7, gh.issue_comments(7), NEW)

    def test_the_dispatcher_reads_the_journal_with_the_aliases(self):
        import devloop.__main__ as devloop_main
        from devloop import apps
        cfg = renamed(CFG)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(devloop_main, 'load_config', return_value=cfg), \
                mock.patch.object(devloop_main, 'main_checkout', return_value=Path(tmp)), \
                mock.patch.object(devloop_main.Identities, 'from_config',
                                  return_value=apps.Identities(cfg.repo, {})):
            args = SimpleNamespace(config=None, local_config=None, command='status',
                                   dry_run=False)
            _, _, d = devloop_main.build(args)
        self.assertEqual(d.ledger.journal.aliases, {OLD: NEW})
        self.assertEqual(d.ledger.journal.repo_name(OLD), NEW)
        self.assertEqual(d.ledger.journal.repo_name('other/repo'), 'other/repo')


if __name__ == '__main__':
    unittest.main()
