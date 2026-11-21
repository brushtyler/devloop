"""The directories of the CLIs of the agent user (issue #517).

``~/.claude``, ``~/.codex``, ``~/.gemini``, ``~/.gemini/config`` and ``~/.gemini/antigravity-cli``
of the agent user are root's, of its group, mode 1770: the agent user creates and replaces its own entries there
(credentials, locks, temporary files), but cannot rename nor remove those of root, and a sealed
entry that does not exist is a placeholder of root it cannot create. The directories may hold
only the entries of the list of the code (``agentuser.CLI_ALLOWED``): an unknown one refuses the
turn with an escalation ``configuration`` that names it. What a CLI would read again as context
in a later session is removed before its turns (agy, ``CLI_CLEARED``), turned off (the auto
memory of Claude Code; the memories of codex, ``[features] memories = false`` in its sealed
configuration) or sealed (``~/.codex/memories``). ``[claude_token]`` of #507 is gone.

sudo is never run here: the launch mechanism is a fake, or runs the commands as the current
user (``LocalUser``), which stands for the agent user; root's entries are those of another
user, such as the system's temporary directory.
"""
import dataclasses
import errno
import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import agents, agentuser, config, markdown
from devloop.agentuser import AgentUser, AgentUserError
from devloop.context import _cli_state_refused, _start_failure
from devloop.dispatcher import Dispatcher
from devloop.planning import Action
from helpers import ssdlc_project
from helpers.devloop_fakes import FakeGH
from test_devloop import FakeWS, raw_review
from test_devloop_agent_user import LocalUser, cli_dirs, cli_listing

CFG = dataclasses.replace(ssdlc_project.load(), max_parallel_actions=1)
HOME = Path('/home/devloop-agent')


def sticky_of_another():
    """The system's temporary directory when it is as the directories of the CLIs are on the
    host for the agent user: sticky, writable, of another user (root). None otherwise."""
    tmp = Path(tempfile.gettempdir())
    info = tmp.stat()
    if (os.geteuid() == 0 or not info.st_mode & 0o1000 or info.st_uid == os.geteuid()
            or not os.access(tmp, os.W_OK | os.X_OK)):
        return None
    return tmp


class StickyDirectoryTests(unittest.TestCase):
    """What the mode of setup-agent-user.sh gives the agent user, with the real kernel: a
    sticky directory of another user it may write, as the system's temporary directory."""

    def setUp(self):
        self.dir = sticky_of_another()
        if self.dir is None:
            self.skipTest('no sticky directory of another user to try in')
        self.tag = f'devloop-517-{os.getpid()}-{threading.get_ident()}'
        self.made = []
        self.addCleanup(self.clean)

    def clean(self):
        for path in self.made:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def path(self, name):
        path = self.dir / f'{self.tag}-{name}'
        self.made.append(path)
        return path

    def test_the_agent_user_creates_renames_and_replaces_its_credentials(self):
        # The startup check accepts such a directory as a directory of the CLIs.
        self.assertEqual(LocalUser(self.dir).probe([('cli', self.dir)]), [])
        # Claude Code: a temporary file renamed over .credentials.json.
        credentials = self.path('.credentials.json')
        credentials.write_text('old login\n')
        renewed = self.path('.credentials.json.tmp')
        renewed.write_text('renewed login\n')
        os.rename(renewed, credentials)
        self.assertEqual(credentials.read_text(), 'renewed login\n')
        # Codex: auth.lock created, a temporary .auth-XXXX renamed over auth.json.
        lock = self.path('auth.lock')
        os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        auth = self.path('auth.json')
        auth.write_text('{"tokens": "old"}\n')
        temporary = self.path('.auth-Xa12')
        with open(temporary, 'w') as handle:
            handle.write('{"tokens": "new"}\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, auth)
        self.assertEqual(json.loads(auth.read_text()), {'tokens': 'new'})
        # And it removes its own entries, a lock, a temporary file.
        os.unlink(lock)
        self.assertFalse(lock.exists())

    def test_the_agent_user_cannot_create_a_placeholder_nor_rename_or_remove_it(self):
        # A placeholder of root: an entry of another user in the sticky directory.
        others = [p for p in self.dir.iterdir()
                  if p.lstat().st_uid != os.geteuid() and not p.is_symlink()]
        if not others:
            self.skipTest(f'no entry of another user in {self.dir}')
        placeholder = others[0]
        found = {code for code, _ in LocalUser(self.dir).probe([('sealed', placeholder)])}
        self.assertFalse(found & {'c', 'm', 'w', 'o'}, found)  # neither replaced nor written
        with self.assertRaises(FileExistsError):  # its name cannot be created again
            os.close(os.open(placeholder, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        self.assertFalse(os.access(placeholder, os.W_OK))
        # Without the placeholder the agent user could create the sealed entry: the startup
        # check refuses that.
        missing = self.dir / f'{self.tag}-CLAUDE.md'
        self.assertIn(('c', str(missing)), LocalUser(self.dir).probe([('sealed', missing)]))


class AclMigrationTests(unittest.TestCase):
    """The ACL of ~/.claude and ~/.codex of an earlier setup-agent-user.sh, with the real
    ``setfacl`` (1/codex-reviewer/R1-1): ``chmod 1770`` changes its mask, not the entry of the
    group, and only ``setfacl -m g::rwx``, as the script runs it, lets the group write."""

    SCRIPT = Path(__file__).resolve().parent.parent / 'ops/host/setup-agent-user.sh'

    def acl(self, path):
        done = subprocess.run(['getfacl', '-p', '--omit-header', str(path)],
                              capture_output=True, text=True, check=True)
        return [line.split('#')[0].strip() for line in done.stdout.splitlines() if line]

    def test_the_group_gets_write_also_where_an_acl_was(self):
        if not (shutil.which('setfacl') and shutil.which('getfacl')):
            self.skipTest('no setfacl or getfacl')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / '.claude'
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        me = pwd.getpwuid(os.geteuid()).pw_name
        # The earlier setup: 755 and an entry for devloop that only traverses.
        if subprocess.run(['setfacl', '-m', f'u:{me}:--x', str(path)],
                          capture_output=True).returncode != 0:
            self.skipTest('no ACL on this file system')
        self.assertIn('group::r-x', self.acl(path))
        path.chmod(0o1770)  # what `owned` does
        self.assertIn('group::r-x', self.acl(path))  # the group still cannot write
        self.assertIn('mask::rwx', self.acl(path))
        subprocess.run(['setfacl', '-m', 'g::rwx', str(path)], check=True)
        acl = self.acl(path)
        self.assertIn('group::rwx', acl)
        self.assertIn('mask::rwx', acl)
        self.assertIn(f'user:{me}:--x', acl)  # devloop still only traverses
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o1770)
        self.assertIn('ensure_acl "$AGENT_HOME/$dir" "g::rwx" "group::rwx"',
                      self.SCRIPT.read_text())


class ListTests(unittest.TestCase):
    """``cli_state_problems``: what the listing of the agent user shows."""

    def problems(self, entries=(), denied=(), records=None):
        records = cli_listing(entries) if records is None else records
        return agentuser.cli_state_problems(HOME, records, denied)

    def test_the_entries_of_the_list_and_the_sealed_ones_of_root_are_accepted(self):
        entries = [('.claude', 'f', 1001, '.credentials.json'),
                   ('.claude', 'f', 1001, '.credentials.json.tmp.4242.1700000000'),
                   ('.claude', 'd', 1001, 'projects'), ('.claude', 'f', 0, 'CLAUDE.md'),
                   ('.claude', 'd', 0, 'skills'),
                   ('.codex', 'f', 1001, 'auth.json'), ('.codex', 'f', 1001, 'auth.lock'),
                   ('.codex', 'f', 1001, 'auth.json.lock'), ('.codex', 'f', 1001, '.auth-Xa12'),
                   ('.codex', 'f', 1001, 'memories_1.sqlite-wal'),
                   ('.codex', 'd', 0, 'plugins'), ('.codex', 'f', 0, 'AGENTS.md'),
                   ('.gemini', 'f', 0, 'GEMINI.md'),
                   ('.gemini/config', 'f', 1001, '.migrated'),
                   ('.gemini/config', 'f', 0, 'config.json'),
                   ('.gemini/config', 'f', 0, 'mcp_config.json'),
                   ('.gemini/config', 'd', 0, 'projects'),
                   ('.gemini/antigravity-cli', 'f', 1001, 'antigravity-oauth-token'),
                   ('.gemini/antigravity-cli', 'f', 1001,
                    'jetbox_summaries_proto.pb.123abc.tmp'),
                   ('.gemini/antigravity-cli', 'd', 1001, 'bin')]
        self.assertEqual(self.problems(entries), [])

    def test_an_unknown_entry_is_named(self):
        problems = self.problems([('.codex', 'f', 1001, 'hooks.json'),
                                  ('.gemini/antigravity-cli', 'd', 1001, 'mcp')])
        self.assertEqual(len(problems), 2)
        self.assertIn(f"{HOME}/.codex holds 'hooks.json' (type 'f'), which is not in the list",
                      problems[0])
        self.assertIn(f"{HOME}/.gemini/antigravity-cli holds 'mcp' (type 'd')", problems[1])

    def test_a_sealed_entry_the_agent_user_made_is_refused(self):
        for where, name in (('.claude', 'CLAUDE.md'), ('.codex', 'AGENTS.md'),
                            ('.gemini', 'GEMINI.md'), ('.codex', 'plugins'),
                            ('.gemini/config', 'config.json'),
                            ('.gemini/config', 'mcp_config.json'),
                            ('.gemini/config', 'projects')):
            with self.subTest(name=name):
                problems = self.problems([(where, 'f', 1001, name)])
                self.assertEqual(problems, [f"{HOME / where / name} is a sealed entry that is "
                                            "not root's (owner 1001): the agent user made it"])

    def test_an_entry_of_another_type_or_a_link_is_refused(self):
        problems = self.problems([('.codex', 'l', 1001, 'auth.json'),
                                  ('.claude', 'f', 1001, 'projects')])
        self.assertEqual(len(problems), 2)
        self.assertIn("holds 'projects' of type 'f'", problems[0])
        self.assertIn("holds 'auth.json' of type 'l', where the list of the entries of the CLIs "
                      "wants 'f'", problems[1])

    def test_the_cleanup_mark_of_claude_is_accepted_as_a_file(self):
        # #525: Claude Code (2.1.326) writes the date of its periodic cleanup in
        # ~/.claude/.last-cleanup at each start: a file of the agent user, nothing else.
        self.assertEqual(self.problems([('.claude', 'f', 1001, '.last-cleanup')]), [])
        self.assertNotIn('.claude/.last-cleanup', agentuser.CLI_CLEARED)
        self.assertNotIn('.claude/.last-cleanup', dict(agentuser.CLI_LINKS))
        for kind in ('d', 'l'):
            with self.subTest(kind=kind):
                entry = ('.claude', kind, 1001, '.last-cleanup') + (('x',) if kind == 'l' else ())
                problems = self.problems([entry])
                self.assertEqual(len(problems), 1)
                self.assertIn("holds '.last-cleanup'", problems[0])

    def test_the_log_link_of_agy_into_its_log_directory_is_accepted(self):
        # #525: agy makes cli.log at each start a link to the log of the day.
        for target in ('log/cli-20261121_132524.log', 'log/cli.log'):
            with self.subTest(target=target):
                self.assertEqual(self.problems([('.gemini/antigravity-cli', 'l', 1001,
                                                 'cli.log', target)]), [])
        self.assertEqual(self.problems([('.gemini/antigravity-cli', 'f', 1001, 'cli.log')]),
                         [])

    def test_a_log_link_of_agy_that_leads_elsewhere_is_refused(self):
        # #525: an absolute path, a '..', another directory, a deeper path, the directory itself.
        where = f'{HOME}/.gemini/antigravity-cli'
        for target in ('/home/devloop-agent/.gemini/antigravity-cli/log/cli-1.log',
                       '/etc/passwd', '../antigravity-cli/log/cli-1.log', 'log/../cli-1.log',
                       'log/..', 'log/.', 'log/', 'log', 'log//cli-1.log', 'log/sub/cli-1.log',
                       './log/cli-1.log', 'brain/cli-1.log', 'logs/cli-1.log', 'cli-1.log',
                       '../../.claude/CLAUDE.md', ''):
            with self.subTest(target=target):
                problems = self.problems([('.gemini/antigravity-cli', 'l', 1001, 'cli.log',
                                           target)])
                self.assertEqual(problems, [f"{where} holds 'cli.log', a symbolic link to "
                                            f"{target!r}, which does not lead to a file of log/ "
                                            "next to it with a relative path (CLI_LINKS of "
                                            "devloop/agentuser.py)"])
        # A link whose target the listing does not give is refused too.
        self.assertEqual(self.problems([('.gemini/antigravity-cli', 'l', 1001, 'cli.log')]),
                         [f"{where} holds 'cli.log', a symbolic link whose target could not "
                          "be read"])
        # Any other name stays refused as a link, also one into log/.
        problems = self.problems([('.gemini/antigravity-cli', 'l', 1001, 'installation_id',
                                   'log/cli-1.log')])
        self.assertEqual(len(problems), 1)
        self.assertIn("holds 'installation_id' of type 'l'", problems[0])
        # Taken away by the configuration, it is refused in any form.
        problems = self.problems([('.gemini/antigravity-cli', 'l', 1001, 'cli.log',
                                   'log/cli-1.log')],
                                 denied=('.gemini/antigravity-cli/cli.log',))
        self.assertIn("holds 'cli.log' (type 'l'), which is not in the list", problems[0])

    def test_link_allowed(self):
        self.assertTrue(agentuser.link_allowed('log/cli-20261121_132524.log', 'log'))
        for target in ('/log/x', '../log/x', 'log/..', 'log/sub/x', 'log/', 'x', 'logs/x'):
            self.assertFalse(agentuser.link_allowed(target, 'log'), target)

    def test_a_directory_of_the_clis_the_agent_user_replaced_is_refused(self):
        records = (b'D:.claude\0D:.codex\0D:.gemini\0e:d:1001:config\0'
                   b'e:d:1001:antigravity-cli\0D:.gemini/config\0D:.gemini/antigravity-cli\0E:\0')
        self.assertEqual(self.problems(records=records),
                         [f"{HOME}/.gemini/config is a directory of the CLIs that is "
                          "not root's (owner 1001): the agent user made it",
                          f"{HOME}/.gemini/antigravity-cli is a directory of the CLIs that is "
                          "not root's (owner 1001): the agent user made it"])

    def test_a_listing_that_does_not_end_or_misses_a_directory_is_refused(self):
        self.assertEqual(self.problems(records=b'D:.claude\0D:.codex\0'),
                         [f'{HOME}/.gemini was not listed',
                          f'{HOME}/.gemini/config was not listed',
                          f'{HOME}/.gemini/antigravity-cli was not listed',
                          f'the listing of the directories of the CLIs in {HOME} did not end'])
        self.assertEqual(self.problems(records=b'')[-1],
                         f'the listing of the directories of the CLIs in {HOME} did not end')
        records = (b'L:.claude\0M:.codex\0N:.gemini\0D:.gemini/config\0'
                   b'D:.gemini/antigravity-cli\0E:\0')
        self.assertEqual(self.problems(records=records),
                         [f'{HOME}/.claude is a symbolic link', f'{HOME}/.codex is missing',
                          f'{HOME}/.gemini is not a directory'])

    def test_the_configuration_only_takes_entries_away(self):
        entries = [('.codex', 'f', 1001, 'memories_1.sqlite')]
        self.assertEqual(self.problems(entries), [])
        problems = self.problems(entries, denied=('.codex/memories_1.sqlite*',))
        self.assertIn("holds 'memories_1.sqlite'", problems[0])
        with self.assertRaisesRegex(ValueError, 'may only take entries away'):
            self.problems(denied=('.codex/hooks.json',))

    def test_the_lists_are_consistent(self):
        allowed = dict(agentuser.CLI_ALLOWED)
        self.assertEqual(len(allowed), len(agentuser.CLI_ALLOWED))  # no entry twice
        links = dict(agentuser.CLI_LINKS)
        self.assertEqual(len(links), len(agentuser.CLI_LINKS))
        for entry, types in agentuser.CLI_ALLOWED:
            self.assertIn(entry.rpartition('/')[0], agentuser.CLI_DIRS, entry)
            self.assertTrue(types and set(types) <= {'f', 'd', 'l'}, entry)
            # A link only with its rule (#525), into a directory of the list next to it.
            self.assertEqual('l' in types, entry in links, entry)
            if entry in links:
                inside = f'{entry.rpartition("/")[0]}/{links[entry]}'
                self.assertEqual(allowed.get(inside), 'd', entry)
            self.assertNotIn(entry, agentuser.CLI_SEALED)
        self.assertEqual(agentuser.CLI_LINKS, (('.gemini/antigravity-cli/cli.log', 'log'),))
        for entry in agentuser.CLI_CLEARED:  # what is removed is among the entries allowed
            self.assertIn(entry, allowed)
        for entry in agentuser.CLI_WRITABLE:
            self.assertIn(entry, allowed)
        self.assertIn('.codex/plugins', agentuser.CLI_SEALED)
        self.assertIn('.codex/memories', agentuser.CLI_SEALED)
        # agy's configuration: its plugins, MCP servers and projects sealed, only the mark of
        # its migration the agent user's, and nothing cleared there (it is sealed instead).
        self.assertIn('.gemini/config', agentuser.CLI_DIRS)
        self.assertLess(agentuser.CLI_DIRS.index('.gemini'),
                        agentuser.CLI_DIRS.index('.gemini/config'))
        for name in ('config.json', 'mcp_config.json', 'projects'):
            self.assertIn(f'.gemini/config/{name}', agentuser.CLI_SEALED)
        self.assertEqual({e: t for e, t in agentuser.CLI_ALLOWED
                          if e.startswith('.gemini/config/')},
                         {'.gemini/config/.migrated': 'f'})
        self.assertFalse([p for p in agentuser.CLI_CLEARED if p.startswith('.gemini/config')])
        self.assertTrue(agentuser.placeholder_is_file('.gemini/config/mcp_config.json'))
        self.assertFalse(agentuser.placeholder_is_file('.gemini/config/projects'))
        self.assertFalse(agentuser.placeholder_is_file('.codex/plugins'))
        self.assertFalse(agentuser.placeholder_is_file('.local'))
        self.assertTrue(agentuser.placeholder_is_file('.claude/CLAUDE.md'))
        self.assertTrue(agentuser.placeholder_is_file('.codex/AGENTS.override.md'))


class RealProbeTests(unittest.TestCase):
    """The real listing and clearing, as the current user."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / 'home'
        cli_dirs(self.home)
        self.user = LocalUser(self.home)

    def make(self, *names, directory=False):
        for name in names:
            path = self.home / name
            if directory:
                path.mkdir()
                (path / 'inside.md').write_text('a memory\n')
            else:
                path.write_text('x\n')

    def test_a_home_with_the_entries_of_the_list_passes(self):
        self.make('.claude/.credentials.json', '.codex/auth.json', '.codex/auth.lock',
                  '.claude/CLAUDE.md', '.gemini/antigravity-cli/cli.log')
        self.make('.claude/projects', '.codex/plugins', directory=True)
        self.assertEqual(self.user.cli_state(), [])

    def test_the_log_link_of_agy_is_read_with_its_target(self):
        # #525: the real listing gives the target of the link, as agy makes it.
        agy = self.home / '.gemini/antigravity-cli'
        (agy / 'log').mkdir()
        (agy / 'log/cli-20261121_132524.log').write_text('started\n')
        link = agy / 'cli.log'
        link.symlink_to('log/cli-20261121_132524.log')
        self.assertEqual(self.user.cli_state(), [])
        outside = self.tmp / 'elsewhere.log'
        outside.write_text('x\n')
        for target in (str(outside), '../antigravity-cli/log/cli-20261121_132524.log',
                       'brain/cli.log'):
            with self.subTest(target=target):
                link.unlink()
                link.symlink_to(target)
                problems = self.user.cli_state()
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"holds 'cli.log', a symbolic link to {target!r}, which does not "
                              "lead to a file of log/", problems[0])

    def test_an_unknown_entry_is_found_with_its_name(self):
        self.make('.codex/hooks.json')
        (self.home / '.claude/odd\nname').write_text('x\n')
        problems = self.user.cli_state()
        self.assertEqual(len(problems), 2)
        self.assertIn(f"{self.home}/.claude holds 'odd\\nname' (type 'f')", problems[0])
        self.assertIn(f"{self.home}/.codex holds 'hooks.json' (type 'f')", problems[1])

    def test_the_memories_of_agy_are_cleared_and_nothing_else(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'keep.md').write_text('not the CLI\'s\n')
        self.make('.codex/memories_1.sqlite', '.codex/memories_1.sqlite-wal',
                  '.codex/auth.json', '.codex/state_5.sqlite',
                  '.gemini/antigravity-cli/conversation_summaries.db',
                  '.gemini/antigravity-cli/jetbox_summaries_proto.pb',
                  '.gemini/antigravity-cli/antigravity-oauth-token', '.gemini/config/.migrated')
        self.make('.gemini/antigravity-cli/brain', '.gemini/antigravity-cli/builtin',
                  '.gemini/antigravity-cli/conversations', directory=True)
        (self.home / '.gemini/antigravity-cli/implicit').symlink_to(outside)
        # A codex turn clears nothing: the memories of codex are off in its configuration
        # (request of the operator on PR #523), and the link left by agy is refused.
        self.assertEqual(agentuser.cleared_for('codex'), ())
        problems = self.user.cli_state(agentuser.cleared_for('codex'))
        self.assertEqual(len(problems), 1)
        self.assertIn("holds 'implicit' of type 'l'", problems[0])
        for kept in ('.codex/memories_1.sqlite', '.codex/memories_1.sqlite-wal',
                     '.codex/auth.json', '.codex/state_5.sqlite',
                     '.gemini/antigravity-cli/brain/inside.md', '.gemini/config/.migrated'):
            self.assertTrue((self.home / kept).exists(), kept)
        # An agy turn: clearing removes the link, never what it leads to.
        self.assertEqual(self.user.cli_state(agentuser.cleared_for('agy')), [])
        for gone in ('brain', 'builtin', 'implicit', 'conversation_summaries.db',
                     'jetbox_summaries_proto.pb'):
            self.assertFalse(os.path.lexists(self.home / '.gemini/antigravity-cli' / gone), gone)
        self.assertTrue((outside / 'keep.md').exists())
        # ~/.gemini/config is a directory of the CLIs, with its configuration sealed: it stays,
        # as the mark of agy's migration in it.
        self.assertTrue((self.home / '.gemini/config/.migrated').exists())
        for kept in ('conversations/inside.md', 'antigravity-oauth-token'):
            self.assertTrue((self.home / '.gemini/antigravity-cli' / kept).exists(), kept)
        self.assertTrue((self.home / '.codex/memories_1.sqlite').exists())
        self.assertEqual(agentuser.cleared_for('claude'), ())

    def test_the_configuration_of_agy_written_by_the_agent_user_is_refused(self):
        # ~/.gemini/config: a turn that writes an MCP server or enables a plugin there would
        # have the agy turns after it load them (#438). Written by the agent user, the turn is
        # refused before it starts, and so is the start.
        config_dir = self.home / '.gemini/config'
        server = {'mcpServers': {'x': {'command': '/tmp/x'}}}
        (config_dir / 'mcp_config.json').write_text(json.dumps(server))
        (config_dir / 'config.json').write_text('{"plugins": {"swarm": {"enabled": true}}}')
        (config_dir / '.migrated').write_text('')
        found = set(self.user.probe(agentuser.cli_items(self.home)))
        for name in ('mcp_config.json', 'config.json'):
            with self.subTest(name=name):
                self.assertIn(('w', str(config_dir / name)), found)
                self.assertIn(('o', str(config_dir / name)), found)
        # Without its placeholder, ``projects`` could be created by the agent user.
        self.assertIn(('c', str(config_dir / 'projects')), found)
        self.assertNotIn(('w', str(config_dir / '.migrated')), found)  # not probed: allowed
        # The listing before each turn: the files are the agent user's, not root's.
        records = cli_listing([('.gemini/config', 'f', 1001, 'mcp_config.json'),
                               ('.gemini/config', 'f', 1001, 'config.json'),
                               ('.gemini/config', 'f', 1001, '.migrated')])
        problems = agentuser.cli_state_problems(HOME, records)
        self.assertEqual(problems, [
            f"{HOME}/.gemini/config/mcp_config.json is a sealed entry that is not root's "
            "(owner 1001): the agent user made it",
            f"{HOME}/.gemini/config/config.json is a sealed entry that is not root's "
            "(owner 1001): the agent user made it"])
        # Nothing of agy clears them: they stay as root sealed them.
        self.assertEqual(self.user.cli_state(agentuser.cleared_for('agy')), [])
        self.assertTrue((config_dir / 'mcp_config.json').exists())

    def test_a_failure_to_list_or_clear_is_an_error(self):
        def run(argv, **kw):
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'sudo: a password is required')
        with self.assertRaisesRegex(AgentUserError, 'cannot list or clear .* a password'):
            AgentUser('devloop-agent', home=str(HOME), run=run).cli_state()

    def test_the_listing_runs_as_the_agent_user_with_the_system_tools(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=0, stdout=cli_listing(), stderr=b'')
        user = AgentUser('devloop-agent', home=str(HOME), run=run)
        self.assertEqual(user.cli_state(('.gemini/antigravity-cli/brain',)), [])
        argv, kw = calls[0]
        self.assertEqual(argv[:8], ['sudo', '-n', '-E', '-u', 'devloop-agent', '--',
                                    '/usr/bin/bash', '-c'])
        self.assertTrue(argv[8].startswith('PATH=/usr/bin:/bin\n'))
        self.assertEqual(argv[9:], ['cli-state', str(HOME), 'x:.gemini/antigravity-cli/brain',
                                    *(f'l:{d}' for d in agentuser.CLI_DIRS)])
        self.assertEqual(kw['env']['HOME'], str(HOME))


class RunnerTests(unittest.TestCase):
    """Before every turn the runner lists the directories, as the agent user."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.entries = []
        self.states = []
        self.launched = []
        self.inner = None

        def run(argv, **kw):
            if 'cli-state' in argv:
                self.states.append([a[2:] for a in argv if a.startswith('x:')])
                return SimpleNamespace(returncode=0, stdout=cli_listing(self.entries),
                                       stderr=b'')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        self.user = AgentUser('devloop-agent', home=str(HOME), run=run)

    def cli(self, argv, **kw):
        self.launched.append(argv)
        if self.inner:
            inner, self.inner = self.inner, None
            inner()
        if 'codex' in argv:
            Path(argv[argv.index('-o') + 1]).write_text(json.dumps(raw_review()))
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if 'agy' in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {'status': 'SUCCESS', 'structured_output': raw_review()}), stderr='')
        return SimpleNamespace(returncode=0, stdout=json.dumps(
            {'is_error': False, 'structured_output': raw_review()}), stderr='')

    def runner(self):
        runner = agents.AgentRunner(self.tmp / 'logs', 10, run=self.cli,
                                    env={'HOME': str(HOME)}, agent_user=self.user,
                                    shared_dir=self.tmp / 'agent-shared')
        runner._check_agy_bin = lambda agent: None  # #494, tested on its own
        return runner

    def turn(self, runner, kind, label='pr-1'):
        return runner.run(CFG.agent(kind), 'review', 'p', 'review', self.tmp, label)

    def test_an_unknown_entry_refuses_the_turn_before_it_starts(self):
        self.entries = [('.codex', 'f', 1001, 'hooks.json')]
        runner = self.runner()
        for kind in ('claude', 'codex', 'agy'):  # every CLI, whichever directory holds it
            with self.subTest(kind=kind):
                with self.assertRaises(agents.CliStateRefused) as ctx:
                    self.turn(runner, kind)
                self.assertEqual(self.launched, [])
                message = str(ctx.exception)
                self.assertIn('turn refused before it started', message)
                self.assertIn(f"{HOME}/.codex holds 'hooks.json'", message)
                self.assertTrue(_cli_state_refused(ctx.exception))
                self.assertFalse(_start_failure(ctx.exception))  # not a tool_failure
        self.entries = []
        self.assertEqual(self.turn(runner, 'codex')['verdict'], 'approve')  # once removed

    def test_a_listing_that_fails_refuses_the_turn(self):
        self.user._run = lambda argv, **kw: SimpleNamespace(returncode=1, stdout=b'',
                                                             stderr=b'find: denied')
        with self.assertRaisesRegex(agents.CliStateRefused, 'find: denied'):
            self.turn(self.runner(), 'claude')
        self.assertEqual(self.launched, [])

    def test_the_memories_of_the_cli_of_the_turn_are_cleared_before_it(self):
        runner = self.runner()
        for kind in ('codex', 'agy', 'claude'):
            self.turn(runner, kind)
        self.assertEqual(self.states, [[], list(agentuser.cleared_for('agy')), []])
        self.assertIn('.gemini/antigravity-cli/brain', self.states[1])

    def blocked_turn(self, runner, kind):
        """A turn of ``kind`` in a thread, held inside its CLI until the returned event."""
        started, release = threading.Event(), threading.Event()

        def hold():
            started.set()
            release.wait(30)
        self.inner = hold
        thread = threading.Thread(target=self.turn, args=(runner, kind))
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(30))
        return thread, release

    def test_the_turns_of_a_cli_with_memories_run_one_at_a_time(self):
        # 1/codex-reviewer/R1-2: a second agy turn would read what the first one writes in
        # the memories of agy while both run: it waits for the first, then clears them.
        runner = self.runner()
        first, release = self.blocked_turn(runner, 'agy')
        second = threading.Thread(target=self.turn, args=(runner, 'agy', 'pr-2'))
        second.start()
        self.addCleanup(second.join, 30)
        # Meanwhile claude and codex, which keep no memory to clear, run.
        self.turn(runner, 'claude', label='pr-3')
        self.turn(runner, 'codex', label='pr-4')
        second.join(0.5)
        self.assertTrue(second.is_alive())  # still waiting
        self.assertEqual(len(self.launched), 3)
        self.assertEqual(self.states, [list(agentuser.cleared_for('agy')), [], []])
        release.set()
        first.join(30)
        second.join(30)
        self.assertFalse(second.is_alive())
        self.assertEqual(len(self.launched), 4)
        # The second agy turn cleared the memories the first one may have left.
        self.assertEqual(self.states[-1], list(agentuser.cleared_for('agy')))
        self.assertEqual(runner._running, {'codex': 0, 'claude': 0, 'agy': 0})

    def test_the_turns_of_codex_run_together(self):
        # Request of the operator on PR #523: the memories of codex are off in its
        # configuration (checked at the start), so its turns are neither cleared nor
        # serialized: a second codex turn starts while the first one runs.
        runner = self.runner()
        first, release = self.blocked_turn(runner, 'codex')
        self.assertEqual(self.turn(runner, 'codex', label='pr-2')['verdict'], 'approve')
        self.assertTrue(first.is_alive())  # the first one still runs
        self.assertEqual(len(self.launched), 2)
        self.assertEqual(self.states, [[], []])
        release.set()
        first.join(30)
        self.assertEqual(runner._running, {'codex': 0})

    def test_a_turn_cancelled_while_it_waits_never_starts(self):
        runner = self.runner()
        first, release = self.blocked_turn(runner, 'agy')
        cancel, outcome = threading.Event(), []

        def waiting():
            with runner.cancellable(cancel):
                try:
                    self.turn(runner, 'agy', label='pr-2')
                except agents.TurnCancelled as exc:
                    outcome.append(exc)
        second = threading.Thread(target=waiting)
        second.start()
        self.addCleanup(second.join, 30)
        second.join(0.5)
        self.assertTrue(second.is_alive())  # waiting for the first agy turn
        cancel.set()
        second.join(30)
        self.assertFalse(second.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIn('cancelled before it started', str(outcome[0]))
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(len(self.states), 1)  # neither listed nor cleared
        release.set()
        first.join(30)
        self.assertEqual(runner._running, {'agy': 0})

    def test_a_refused_or_failed_turn_does_not_stay_running(self):
        runner = self.runner()

        def fail(argv, **kw):
            raise OSError(errno.ENOENT, 'no such file')
        runner._run = fail
        runner._sleep = lambda seconds: None
        with self.assertRaises(agents.StartFailure):
            self.turn(runner, 'codex')
        self.assertEqual(runner._running, {'codex': 0})

    def test_the_auto_memory_of_claude_is_off_in_every_turn(self):
        # The memory of claude, in ~/.claude/projects, is turned off rather than cleared.
        self.assertNotIn('.claude/projects', agentuser.CLI_CLEARED)
        self.assertEqual(agents.CLAUDE_TURN_ENV, {'CLAUDE_CODE_DISABLE_AUTO_MEMORY': '1'})
        self.assertFalse(agents.CLAUDE_TURN_SETTINGS['autoMemoryEnabled'])
        self.turn(self.runner(), 'claude')
        argv = self.launched[0]
        self.assertEqual(json.loads(argv[argv.index('--settings') + 1]),
                         agents.CLAUDE_TURN_SETTINGS)


class EscalationTests(unittest.TestCase):
    def test_a_refused_turn_is_a_configuration_escalation_that_names_the_entry(self):
        gh = FakeGH()
        error = agents.CliStateRefused(
            f"claude: turn refused before it started, the directories of the CLIs of "
            f"devloop-agent hold what devloop does not admit: {HOME}/.codex holds "
            "'hooks.json' (type 'f'), which is not in the list")
        d = Dispatcher(CFG, gh, FakeWS(), None, log=lambda m: None)
        d._escalate_failure(Action('implement', 7), None, error)
        body = gh.comments[-1][1]
        notice = markdown.read_marker(body, 'notice')
        self.assertEqual((notice['class'], notice['critical']), ('configuration', True))
        self.assertEqual(gh.labels[7], {'agent:needs-operator'})
        self.assertIn("hooks.json", body)
        self.assertIn('Directory delle CLI con lo sticky bit (#517)', body)

    def test_a_wrapped_refusal_is_a_configuration_too(self):
        wrapped = agents.ModelsUnavailable('claude', [('m', 'x')])
        wrapped.__cause__ = agents.CliStateRefused('claude: turn refused')
        gh = FakeGH()
        d = Dispatcher(CFG, gh, FakeWS(), None, log=lambda m: None)
        d._escalate_failure(Action('implement', 7), None, wrapped)
        self.assertEqual(markdown.read_marker(gh.comments[-1][1], 'notice')['class'],
                         'configuration')


class CodexMemoriesTests(unittest.TestCase):
    """Request of the operator on PR #523: the memories of codex (``memories_1.sqlite``) are
    off in so many words in its sealed configuration, ``[features] memories = false``, and its
    turns run together. The startup check and ``check-host.sh`` (``codex_memories`` of
    ``ops/lib.sh``) refuse a configuration without that line, or that turns them on."""

    CASES = [
        (b'[features]\nmemories = false\n', None),
        (b'model = "gpt-5"\n\n[features]\nshell_tool = true\nmemories = false\n', None),
        (b'features.memories = false\n', None),
        (b'[features]\nmemories = false\n[profiles.fast.features]\nmemories = false\n', None),
        (b'', 'does not set [features] memories = false'),
        (b'model = "gpt-5"\n', 'does not set [features] memories = false'),
        (b'[features]\nshell_tool = true\n', 'does not set [features] memories = false'),
        (b'[features]\nmemories = true\n', 'sets [features] memories = True'),
        (b'[features]\nmemories = "false"\n', "sets [features] memories = 'false'"),
        (b'[features]\nmemories = false\n[profiles.fast.features]\nmemories = true\n',
         'sets [profiles.fast.features] memories = True'),
        (b'[features\nmemories = false\n', 'is not valid TOML'),
        (b'\xff[features]\nmemories = false\n', 'is not valid TOML'),
    ]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_the_configuration_must_turn_the_memories_off(self):
        path = HOME / agentuser.CODEX_CONFIG
        for data, expected in self.CASES:
            with self.subTest(data=data):
                problems = agentuser.codex_config_problems(path, data)
                if expected is None:
                    self.assertEqual(problems, [])
                else:
                    self.assertEqual(len(problems), 1, problems)
                    self.assertTrue(problems[0].startswith(f'{path} {expected}'), problems[0])
                    self.assertIn('memories_1.sqlite', problems[0])
                    self.assertIn('setup-agent-user.sh', problems[0])
        too_large = b'[features]\nmemories = false\n' + b'#' * agentuser.CODEX_CONFIG_LIMIT
        self.assertIn('is larger than', agentuser.codex_config_problems(path, too_large)[0])

    def test_the_shell_refuses_what_devloop_refuses(self):
        # check-host.sh and setup-agent-user.sh use codex_memories of ops/lib.sh.
        lib = Path(__file__).resolve().parents[1] / 'ops/lib.sh'
        path = self.tmp / 'config.toml'
        for data, expected in self.CASES:
            with self.subTest(data=data):
                path.write_bytes(data)
                done = subprocess.run(['/usr/bin/bash', '-c', f'. {lib}; codex_memories "$1"',
                                       '_', str(path)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0 if expected is None else 1, done.stdout)
                if expected is not None:
                    self.assertTrue(done.stdout.startswith(f'{path} '), done.stdout)
        path.unlink()
        done = subprocess.run(['/usr/bin/bash', '-c', f'. {lib}; codex_memories "$1"', '_',
                               str(path)], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (1, f'{path} non esiste\n'))

    def test_it_is_read_as_the_agent_user(self):
        home = self.tmp / 'home'
        cli_dirs(home)
        user = LocalUser(home)
        self.assertIn('cannot read', user.codex_config()[0])  # missing
        (home / agentuser.CODEX_CONFIG).write_bytes(b'[features]\nmemories = false\n')
        self.assertEqual(user.codex_config(), [])
        (home / agentuser.CODEX_CONFIG).write_bytes(b'[features]\nmemories = true\n')
        self.assertIn('sets [features] memories = True', user.codex_config()[0])
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            return SimpleNamespace(returncode=0, stdout=b'[features]\nmemories = false\n',
                                   stderr=b'')
        self.assertEqual(AgentUser('devloop-agent', home=str(HOME), run=run).codex_config(), [])
        argv, kw = calls[0]
        self.assertEqual(argv[:8], ['sudo', '-n', '-E', '-u', 'devloop-agent', '--',
                                    '/usr/bin/bash', '-c'])
        self.assertTrue(argv[8].startswith('PATH=/usr/bin:/bin\n'))
        self.assertEqual(argv[9:], ['codex-config', str(HOME / '.codex/config.toml'),
                                    str(agentuser.CODEX_CONFIG_LIMIT + 1)])

    def test_the_startup_check_refuses_a_configuration_without_it(self):
        state = self.tmp / 'state'
        for name in ('worktrees', 'agent-shared'):
            (state / name).mkdir(parents=True)
        for data, expected in ((b'model = "gpt-5"\n', 'does not set'),
                               (b'[features]\nmemories = true\n', 'sets [features] memories')):
            def run(argv, **kw):
                if 'cli-state' in argv:
                    return SimpleNamespace(returncode=0, stdout=cli_listing(), stderr=b'')
                if 'codex-config' in argv:
                    return SimpleNamespace(returncode=0, stdout=data, stderr=b'')
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            user = AgentUser('devloop-agent', home=str(HOME), run=run)
            with self.subTest(data=data), self.assertRaises(AgentUserError) as ctx:
                agentuser.check(user, self.tmp / 'keys', state, me='devloop')
            self.assertIn(f'{HOME}/.codex/config.toml {expected}', str(ctx.exception))

    def test_the_memories_of_codex_are_kept_sealed_and_allowed(self):
        self.assertIn('.codex/memories', agentuser.CLI_SEALED)
        self.assertIn('.codex/memories_1.sqlite*', dict(agentuser.CLI_ALLOWED))
        self.assertNotIn('.codex/memories_1.sqlite*', agentuser.CLI_CLEARED)


class StartupTests(unittest.TestCase):
    def test_the_startup_check_refuses_an_unknown_entry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / 'state'
        for name in ('worktrees', 'agent-shared'):
            (state / name).mkdir(parents=True)

        def run(argv, **kw):
            if 'cli-state' in argv:
                return SimpleNamespace(returncode=0, stdout=cli_listing(
                    [('.claude', 'd', 1001, 'agents.bak')]), stderr=b'')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        user = AgentUser('devloop-agent', home=str(HOME), run=run)
        with self.assertRaises(AgentUserError) as ctx:
            agentuser.check(user, Path(tmp.name) / 'keys', state, me='devloop')
        self.assertIn(f"{HOME}/.claude holds 'agents.bak' (type 'd')", str(ctx.exception))


class ConfigTests(unittest.TestCase):
    def test_cli_denied_takes_entries_of_the_list_away_and_nothing_else(self):
        data = ssdlc_project.data()
        cfg = config.parse_config(dict(data, agent_user={
            'name': 'devloop-agent', 'cli_denied': ['.codex/memories_1.sqlite*']}))
        self.assertEqual(cfg.agent_user.cli_denied, ('.codex/memories_1.sqlite*',))
        self.assertEqual(CFG.agent_user.cli_denied, ())
        for denied in (['.codex/hooks.json'], ['.claude/CLAUDE.md'], '.codex/auth.json', [3]):
            with self.subTest(denied=denied), self.assertRaises(config.ConfigError) as ctx:
                config.parse_config(dict(data, agent_user={'cli_denied': denied}))
            self.assertIn('agent_user.cli_denied', str(ctx.exception))
        self.assertEqual(config.apply_overlay(data, {'agent_user': {'cli_denied': []}})
                         ['agent_user']['cli_denied'], [])  # a key of the overlay

    def test_the_agent_user_gets_the_entries_denied(self):
        from devloop import __main__ as cli
        cfg = dataclasses.replace(CFG, agent_user=config.AgentUserPolicy(
            name='devloop-agent', cli_denied=('.codex/tmp',)))
        user = cli.agent_user_of(cfg, factory=lambda name, **kw: AgentUser(
            name, home=str(HOME), **kw))
        self.assertEqual(user.cli_denied, ('.codex/tmp',))

    def test_the_claude_token_is_refused_with_its_reason(self):
        data = ssdlc_project.data()
        table = {'enabled': False}
        checks = {
            'config': lambda: config.parse_config(dict(data, claude_token=table)),
            'local config': lambda: config.apply_overlay(data, {'claude_token': table}),
            'project file': lambda: config.compose(data, project={'claude_token': table}),
        }
        for where, load in checks.items():
            with self.subTest(where=where), self.assertRaises(config.ConfigError) as ctx:
                load()
            message = str(ctx.exception)
            self.assertTrue(message.startswith(f'{where}: [claude_token] was removed (#517)'),
                            message)
            self.assertIn('Remove the table from the configuration', message)
            self.assertIn('ops/host/migrate.md', message)
        self.assertFalse(hasattr(config, 'ClaudeTokenPolicy'))
        self.assertFalse(Path(agents.__file__).with_name('claudetoken.py').exists())


if __name__ == '__main__':
    unittest.main()
