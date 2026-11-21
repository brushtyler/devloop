"""The directories of the CLIs of the agent user (issue #517).

``~/.claude``, ``~/.codex``, ``~/.gemini`` and ``~/.gemini/antigravity-cli`` of the agent user
are root's, of its group, mode 1770: the agent user creates and replaces its own entries there
(credentials, locks, temporary files), but cannot rename nor remove those of root, and a sealed
entry that does not exist is a placeholder of root it cannot create. The directories may hold
only the entries of the list of the code (``agentuser.CLI_ALLOWED``): an unknown one refuses the
turn with an escalation ``configuration`` that names it. What a CLI would read again as context
in a later session is removed before its turns (``CLI_CLEARED``), turned off (the auto memory of
Claude Code) or sealed (``~/.codex/memories``). ``[claude_token]`` of #507 is gone.

sudo is never run here: the launch mechanism is a fake, or runs the commands as the current
user (``LocalUser``), which stands for the agent user; root's entries are those of another
user, such as the system's temporary directory.
"""
import dataclasses
import errno
import json
import os
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
                   ('.gemini', 'f', 0, 'GEMINI.md'), ('.gemini', 'd', 1001, 'config'),
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
                            ('.gemini', 'GEMINI.md'), ('.codex', 'plugins')):
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

    def test_a_directory_of_the_clis_the_agent_user_replaced_is_refused(self):
        records = (b'D:.claude\0D:.codex\0D:.gemini\0e:d:1001:antigravity-cli\0'
                   b'D:.gemini/antigravity-cli\0E:\0')
        self.assertEqual(self.problems(records=records),
                         [f"{HOME}/.gemini/antigravity-cli is a directory of the CLIs that is "
                          "not root's (owner 1001): the agent user made it"])

    def test_a_listing_that_does_not_end_or_misses_a_directory_is_refused(self):
        self.assertEqual(self.problems(records=b'D:.claude\0D:.codex\0'),
                         [f'{HOME}/.gemini was not listed',
                          f'{HOME}/.gemini/antigravity-cli was not listed',
                          f'the listing of the directories of the CLIs in {HOME} did not end'])
        self.assertEqual(self.problems(records=b'')[-1],
                         f'the listing of the directories of the CLIs in {HOME} did not end')
        records = b'L:.claude\0M:.codex\0N:.gemini\0D:.gemini/antigravity-cli\0E:\0'
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
        for entry, types in agentuser.CLI_ALLOWED:
            self.assertIn(entry.rpartition('/')[0], agentuser.CLI_DIRS, entry)
            self.assertTrue(types and set(types) <= {'f', 'd'}, entry)
            self.assertNotIn(entry, agentuser.CLI_SEALED)
        for entry in agentuser.CLI_CLEARED:  # what is removed is among the entries allowed
            self.assertIn(entry, allowed)
        for entry in agentuser.CLI_WRITABLE:
            self.assertIn(entry, allowed)
        self.assertIn('.codex/plugins', agentuser.CLI_SEALED)
        self.assertIn('.codex/memories', agentuser.CLI_SEALED)
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

    def test_an_unknown_entry_is_found_with_its_name(self):
        self.make('.codex/hooks.json')
        (self.home / '.claude/odd\nname').write_text('x\n')
        problems = self.user.cli_state()
        self.assertEqual(len(problems), 2)
        self.assertIn(f"{self.home}/.claude holds 'odd\\nname' (type 'f')", problems[0])
        self.assertIn(f"{self.home}/.codex holds 'hooks.json' (type 'f')", problems[1])

    def test_the_memories_of_codex_and_agy_are_cleared_and_nothing_else(self):
        outside = self.tmp / 'outside'
        outside.mkdir()
        (outside / 'keep.md').write_text('not the CLI\'s\n')
        self.make('.codex/memories_1.sqlite', '.codex/memories_1.sqlite-wal',
                  '.codex/auth.json', '.codex/state_5.sqlite',
                  '.gemini/antigravity-cli/conversation_summaries.db',
                  '.gemini/antigravity-cli/jetbox_summaries_proto.pb',
                  '.gemini/antigravity-cli/antigravity-oauth-token')
        self.make('.gemini/antigravity-cli/brain', '.gemini/antigravity-cli/builtin',
                  '.gemini/antigravity-cli/conversations', directory=True)
        (self.home / '.gemini/antigravity-cli/implicit').symlink_to(outside)
        # A codex turn: the memories of codex go, those of agy stay, and the link left there
        # is refused in the listing.
        problems = self.user.cli_state(agentuser.cleared_for('codex'))
        self.assertEqual(len(problems), 1)
        self.assertIn("holds 'implicit' of type 'l'", problems[0])
        self.assertFalse((self.home / '.codex/memories_1.sqlite').exists())
        self.assertFalse((self.home / '.codex/memories_1.sqlite-wal').exists())
        for kept in ('.codex/auth.json', '.codex/state_5.sqlite',
                     '.gemini/antigravity-cli/brain/inside.md'):
            self.assertTrue((self.home / kept).exists(), kept)
        # An agy turn: clearing removes the link, never what it leads to.
        self.assertEqual(self.user.cli_state(agentuser.cleared_for('agy')), [])
        for gone in ('brain', 'builtin', 'implicit', 'conversation_summaries.db',
                     'jetbox_summaries_proto.pb'):
            self.assertFalse(os.path.lexists(self.home / '.gemini/antigravity-cli' / gone), gone)
        self.assertTrue((outside / 'keep.md').exists())
        for kept in ('conversations/inside.md', 'antigravity-oauth-token'):
            self.assertTrue((self.home / '.gemini/antigravity-cli' / kept).exists(), kept)
        self.assertEqual(agentuser.cleared_for('claude'), ())

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
        self.assertEqual(user.cli_state(('.codex/memories_1.sqlite*',)), [])
        argv, kw = calls[0]
        self.assertEqual(argv[:8], ['sudo', '-n', '-E', '-u', 'devloop-agent', '--',
                                    '/usr/bin/bash', '-c'])
        self.assertTrue(argv[8].startswith('PATH=/usr/bin:/bin\n'))
        self.assertEqual(argv[9:], ['cli-state', str(HOME), 'x:.codex/memories_1.sqlite*',
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
        self.assertEqual(self.states, [list(agentuser.cleared_for('codex')),
                                       list(agentuser.cleared_for('agy')), []])
        self.assertIn('.codex/memories_1.sqlite*', self.states[0])
        self.assertIn('.gemini/antigravity-cli/brain', self.states[1])

    def test_not_while_another_turn_of_the_same_cli_runs(self):
        runner = self.runner()
        # While a codex turn runs, a second codex turn starts, then a claude one, then agy.
        self.inner = lambda: [self.turn(runner, kind, label='pr-2')
                              for kind in ('codex', 'agy')]
        self.turn(runner, 'codex')
        self.assertEqual(self.states, [list(agentuser.cleared_for('codex')), [],
                                       list(agentuser.cleared_for('agy'))])
        # Once both ended, the next codex turn clears them again.
        self.turn(runner, 'codex')
        self.assertEqual(self.states[-1], list(agentuser.cleared_for('codex')))
        self.assertEqual(runner._running, {'codex': 0, 'agy': 0})

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
