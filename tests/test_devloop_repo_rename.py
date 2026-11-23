"""devloop: the rename of the repository (#540, ADR 0007, R3).

``repo_aliases``, a trust key of the operator in the overlay, empty by default, lists the names
the repository had before a rename. A link to a comment under one of them counts only when the
dispatcher finds the comment by id in the configured repository, with the API, and its author is
in ``trusted_users``: a comment that is not there (a 404) or of another author is refused, and
without ``repo_aliases`` the old name is another repository. The records of the journal and the
markers written under the old name stay the repository's.
"""

import dataclasses
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import tomllib
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


ROOT = Path(__file__).resolve().parents[1]
MIGRATE = ROOT / 'ops' / 'host' / 'migrate.md'
RENAME_STEP = '## Rinomina del repository in devloop (ADR 0007, R3, #540)'
BASH = shutil.which('bash')
# An overlay of the host before the rename: ``repo`` among the top-level keys, then a table.
OVERLAY = f'''# overlay of the host
repo = "{OLD}"
trusted_users = ["brushtyler"]
repo_lock_dir = "/run/devloop/repos"

[report]
'''


def rename_step():
    """The section of ``migrate.md`` for the rename, up to the next one."""
    text = MIGRATE.read_text(encoding='utf-8')
    start = text.index(f'\n{RENAME_STEP}\n')
    end = text.find('\n## ', start + 1)
    return text[start:] if end < 0 else text[start:end]


def blocks(text):
    """The shell blocks of ``text``, in order, without the indentation of the list items."""
    return [textwrap.dedent(b) for b in re.findall(r'```bash\n(.*?)```', text, re.S)]


def block_with(text, needle):
    """The one shell block of ``text`` that contains ``needle``."""
    found = [b for b in blocks(text) if needle in b]
    assert len(found) == 1, (needle, found)
    return found[0]


def runnable(block, skip=(), replace=()):
    """``block`` without the lines that start with one of ``skip`` (those that need the host),
    with each pair of ``replace`` applied."""
    lines = [line for line in block.splitlines() if not line.startswith(tuple(skip))]
    text = '\n'.join(lines) + '\n'
    for old, new in replace:
        text = text.replace(old, new)
    return text


@unittest.skipIf(BASH is None, 'bash is not installed')
class MigrateTests(unittest.TestCase):
    """The section of ``ops/host/migrate.md`` for the rename: its commands, their order, and the
    overlay and ``params.env`` its commands and its rollback leave, run on temporary copies."""

    def setUp(self):
        self.section = rename_step()
        self.commands = self.section[self.section.index('\n### Comandi\n'):]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def run_block(self, text):
        script = self.tmp / 'step.sh'
        script.write_text(text, encoding='utf-8')
        done = subprocess.run([BASH, str(script)], cwd=self.tmp, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60,
                              env=dict(os.environ, LC_ALL='C.UTF-8'))
        self.assertEqual(done.stderr, '')
        return done.stdout

    def overlay(self):
        """The overlay of the temporary directory, as devloop reads it."""
        with open(self.tmp / 'config.local.toml', 'rb') as f:
            local = tomllib.load(f)
        return config.parse_config(config.apply_overlay(ssdlc_project.data(), local))

    def load_params(self, systemd=None):
        """``ops_load_params`` of ``ops/lib.sh`` on the temporary params.env. The units and the
        sudoers of the host are left out (#576): ``SYSTEMD_DIR`` is ``systemd``, by default an
        empty directory of the test, and the sudoers files are under the temporary directory, so
        another installation of devloop on the host does not change what the test sees."""
        probe = self.tmp / 'probe.sh'
        probe.write_text(f'. {shlex.quote(str(ROOT / "ops" / "lib.sh"))}\n'
                         f'PARAMS_FILE={shlex.quote(str(self.tmp / "params.env"))}\n'
                         'ops_load_params\n'
                         'printf "%s\\n" "$REPO" "$SOURCE_REPO" "$DEVLOOP_CLONE" '
                         '"$SUPERVISOR_CLONE"\n', encoding='utf-8')
        host = self.tmp / 'host'
        env = dict(os.environ, LC_ALL='C.UTF-8',
                   SYSTEMD_DIR=str(systemd or host / 'systemd'),
                   SUDOERS_FILE=str(host / 'sudoers.d' / 'devloop-supervisor'),
                   AGENT_SUDOERS_FILE=str(host / 'sudoers.d' / 'devloop-agent'),
                   COORDINATOR_SUDOERS_FILE=str(host / 'sudoers.d' / 'devloop-coordinator'))
        return subprocess.run([BASH, str(probe)], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60, env=env)

    def params(self):
        """``REPO``, ``SOURCE_REPO``, ``DEVLOOP_CLONE`` and ``SUPERVISOR_CLONE`` as
        ``ops_load_params`` of ``ops/lib.sh`` completes them from the temporary params.env."""
        done = self.load_params()
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.splitlines()

    def test_the_params_do_not_depend_on_the_units_of_the_host(self):
        # #576: on a host where another installation already uses the user devloop, the params
        # of the test are refused only if ops_load_params reads that installation's units.
        (self.tmp / 'params.env').write_text(
            (ROOT / 'ops' / 'params.env.example').read_text(encoding='utf-8'))
        other = self.tmp / 'other-host' / 'devloop@other.service.d'
        other.mkdir(parents=True)
        (other / 'project.conf').write_text('[Service]\nUser=devloop\n', encoding='utf-8')
        done = self.load_params(systemd=other.parent)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("DEVLOOP_USER 'devloop' è già l'utente devloop di un'altra installazione",
                      done.stderr)
        self.assertEqual(self.params()[0], NEW)

    def test_the_commands_in_their_order(self):
        steps = (
            'sudo systemctl stop devloop',
            'kill-session -t =devloop-coordinator',
            'sudo -u devloop -H git -C /home/devloop/ssdlc fetch origin',
            'sudo -u devloop -H git -C /home/devloop/ssdlc checkout --detach origin/main',
            'sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned pin '
            'origin/main',
            'gh api repos/brushtyler/devloop --jq .full_name',
            'sudo -u devloop -H git -C /home/devloop/ssdlc remote set-url origin '
            'https://github.com/brushtyler/devloop.git',
            'sudo -u supervisor -H git -C /home/supervisor/ssdlc remote set-url origin '
            'https://github.com/brushtyler/devloop.git',
            'sudo -u supervisor -H git -C /home/supervisor/ssdlc fetch origin',
            'install -o devloop -m 600 config.local.toml config.local.toml.prima-rinomina',
            'repo_aliases = ["brushtyler/ssdlc"]',
            'cp -p params.env params.env.prima-rinomina',
            "sed -i 's|^REPO=brushtyler/ssdlc$|REPO=brushtyler/devloop|' params.env",
            'sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh --dry-run',
            'sudo bash /root/repos/ssdlc/ops/host/setup-coordinator.sh',
            'sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned apps',
            'sudo bash /root/repos/ssdlc/ops/host/check-host.sh',
            'sudo -u devloop -H python3 /home/devloop/ssdlc/scripts/devloop-pinned tick '
            '--dry-run',
            'sudo systemctl start devloop',
            '**Rollback.**',
        )
        at = [self.commands.find(step) for step in steps]
        for step, index in zip(steps, at):
            with self.subTest(step=step):
                self.assertGreaterEqual(index, 0)
        # Each in its place: the pin of the copy of #540 before the rename and before the overlay
        # that only that copy accepts, the parameters before setup-devloop.sh, the checks before
        # the start.
        self.assertEqual(at, sorted(at))
        # The prerequisites: the pull request merged, the repository not renamed yet (#559).
        before = self.section[:self.section.index('\n### Comandi\n')]
        self.assertIn('**La PR di #540 mergiata**', before)
        self.assertIn('**Il repository ancora `brushtyler/ssdlc`**', before)
        self.assertIn('gh api repos/brushtyler/ssdlc --jq .full_name', before)
        self.assertNotIn('**R2 fatto**', before)
        self.assertNotIn('repos/brushtyler/devloop', before)

    def test_the_pin_comes_before_the_rename(self):
        # #559: after the rename the lookup of the apps' installation under the old name gives
        # 404, so no app mints a token and the pin's smoke test fails until the overlay names
        # the new repository. The pin of the copy of #540 runs before the rename, with the loop
        # stopped, and the overlay changes right after it.
        steps = re.findall(r'^(\d+)\. \*\*(.+?)\*\*', self.commands, re.M)
        titles = {title: int(n) for n, title in steps}
        self.assertEqual([int(n) for n, _ in steps], list(range(1, len(steps) + 1)))
        stop = titles['Fermare il loop']
        pin = titles['Clone di devloop e pin']
        rename = titles['R2, la rinomina']
        overlay = titles["L'overlay"]
        self.assertEqual((stop, pin, rename, overlay), (1, 2, 3, 5))
        # The pin's command is in the pin step, the rename's check in the rename step.
        items = re.split(r'^\d+\. ', self.commands, flags=re.M)
        self.assertIn('devloop-pinned pin origin/main', items[pin])
        self.assertNotIn('devloop-pinned pin', ''.join(items[pin + 1:]).split('**Rollback.**')[0])
        self.assertIn('gh api repos/brushtyler/devloop --jq .full_name', items[rename])
        # The introduction no longer says devloop works before the overlay changes.
        intro = self.section[:self.section.index('\n### Prima\n')]
        self.assertNotIn('continua a funzionare', intro)
        self.assertIn('**devloop non funziona**', intro)
        self.assertIn('404', intro)

    def test_the_rollback_names_its_commands(self):
        rollback = self.commands[self.commands.index('**Rollback.**'):]
        for needed in ('repo_aliases = ["brushtyler/devloop"]',
                       'cp -p /root/repos/ssdlc/ops/params.env.prima-rinomina '
                       '/root/repos/ssdlc/ops/params.env',
                       'sudo bash /root/repos/ssdlc/ops/host/setup-devloop.sh',
                       'sudo -u devloop -H git -C /home/devloop/ssdlc remote set-url origin '
                       'https://github.com/brushtyler/ssdlc.git',
                       'sudo -u supervisor -H git -C /home/supervisor/ssdlc remote set-url '
                       'origin https://github.com/brushtyler/ssdlc.git',
                       'sudo systemctl start devloop', 'setup-coordinator.sh',
                       'gh api repos/brushtyler/ssdlc --jq .full_name',
                       'cat config.local.toml.prima-rinomina > config.local.toml',
                       'scripts/devloop-pinned pin <sha12>'):
            with self.subTest(needed=needed):
                self.assertIn(needed, rollback)

    def test_the_overlay_step_and_its_rollback(self):
        (self.tmp / 'config.local.toml').write_text(OVERLAY, encoding='utf-8')
        before = self.overlay()
        self.assertEqual((before.repo, before.repo_aliases), (OLD, ()))
        # Step 4, without the cd to the state_dir of the host and the copy owned by devloop.
        step = block_with(self.commands, 'install -o devloop -m 600 config.local.toml '
                                         'config.local.toml.prima-rinomina')
        out = self.run_block(runnable(step, skip=('cd ', 'install ')))
        # The two checks print what the step says, then the lines of repo.
        self.assertEqual(out.splitlines(), [
            '1', '0', f'2:repo = "{NEW}"', f'3:repo_aliases = ["{OLD}"]',
            '5:repo_lock_dir = "/run/devloop/repos"'])
        after = self.overlay()
        self.assertEqual((after.repo, after.repo_aliases), (NEW, (OLD,)))
        self.assertFalse((self.tmp / 'config.local.toml.nuovo').exists())
        # Rerun on the overlay already renamed, the first check no longer prints 1 and the
        # file stays as it is.
        renamed_text = (self.tmp / 'config.local.toml').read_text(encoding='utf-8')
        self.assertEqual(self.run_block(runnable(step, skip=('cd ', 'install '))).splitlines()[:2],
                         ['0', '1'])
        self.assertEqual((self.tmp / 'config.local.toml').read_text(encoding='utf-8'),
                         renamed_text)
        # The rollback after a failed step (#559): the copy of before, rewritten in place, and
        # the params.env of before.
        (self.tmp / 'params.env.prima-rinomina').write_text('REPO=brushtyler/ssdlc\n')
        (self.tmp / 'params.env').write_text('REPO=brushtyler/devloop\n')
        rollback = self.commands[self.commands.index('**Rollback.**'):]
        # The step skipped the install owned by devloop: the copy of before, made here.
        (self.tmp / 'config.local.toml.prima-rinomina').write_text(OVERLAY, encoding='utf-8')
        out = self.run_block(runnable(
            block_with(rollback, 'cat config.local.toml.prima-rinomina > config.local.toml'),
            skip=('cd ', 'sudo ', 'gh '), replace=(('/root/repos/ssdlc/ops/', ''),)))
        self.assertEqual(out.splitlines(), [f'2:repo = "{OLD}"',
                                            '4:repo_lock_dir = "/run/devloop/repos"'])
        self.assertEqual((self.tmp / 'config.local.toml').read_text(encoding='utf-8'), OVERLAY)
        back = self.overlay()
        self.assertEqual((back.repo, back.repo_aliases), (OLD, ()))
        self.assertEqual((self.tmp / 'params.env').read_text(), 'REPO=brushtyler/ssdlc\n')
        # After the loop ran under the new name: the old name, with the new one as an alias.
        (self.tmp / 'config.local.toml').write_text(renamed_text, encoding='utf-8')
        out = self.run_block(runnable(block_with(rollback, 'config.local.toml.nuovo'),
                                      skip=('cd ',)))
        self.assertEqual(out.splitlines(), [
            f'2:repo = "{OLD}"', f'3:repo_aliases = ["{NEW}"]',
            '5:repo_lock_dir = "/run/devloop/repos"'])
        back = self.overlay()
        self.assertEqual((back.repo, back.repo_aliases), (OLD, (NEW,)))

    def test_the_params_step(self):
        # The params of the host before the rename: the example with the old name, as it was
        # until #563 moved the host to the paths of devloop.
        example = (ROOT / 'ops' / 'params.env.example').read_text(encoding='utf-8')
        self.assertIn(f'\nREPO={NEW}\n', example)
        (self.tmp / 'params.env').write_text(example.replace(f'\nREPO={NEW}\n', f'\nREPO={OLD}\n'))
        self.assertEqual(self.params(), [OLD, '', '/home/devloop/ssdlc',
                                         '/home/supervisor/ssdlc'])
        # Without DEVLOOP_CLONE and SUPERVISOR_CLONE, the new REPO alone moves the clones.
        original = (self.tmp / 'params.env').read_text(encoding='utf-8')
        (self.tmp / 'params.env').write_text(original.replace(f'\nREPO={OLD}\n',
                                                              f'\nREPO={NEW}\n'))
        self.assertEqual(self.params()[2:], ['/home/devloop/devloop', '/home/supervisor/devloop'])
        (self.tmp / 'params.env').write_text(original)
        # Step 5, without the checkout and the cd of the operator's checkout.
        step = runnable(block_with(self.commands, 'cp -p params.env params.env.prima-rinomina'),
                        skip=('git ', 'cd '))
        out = self.run_block(step)
        self.assertEqual(out.splitlines(), [
            f'7:REPO={NEW}', '{}:DEVLOOP_CLONE=/home/devloop/ssdlc'.format(
                len(original.splitlines()) + 1),
            '{}:SUPERVISOR_CLONE=/home/supervisor/ssdlc'.format(len(original.splitlines()) + 2)])
        self.assertEqual(self.params(), [NEW, '', '/home/devloop/ssdlc',
                                         '/home/supervisor/ssdlc'])
        self.assertEqual((self.tmp / 'params.env.prima-rinomina').read_text(encoding='utf-8'),
                         original)
        # Rerun, nothing changes: the clones already written are not added again.
        renamed_text = (self.tmp / 'params.env').read_text(encoding='utf-8')
        self.run_block(step.replace('cp -p params.env params.env.prima-rinomina\n', ''))
        self.assertEqual((self.tmp / 'params.env').read_text(encoding='utf-8'), renamed_text)


if __name__ == '__main__':
    unittest.main()
