"""devloop: an agy model that runs a denied command (#319): a targeted retry, then the next
fallback model, remembered for ``denied_cooldown_hours``, or an escalation naming the command
and the model."""

import json
import tempfile
import unittest
from pathlib import Path

from devloop import agents, config, markdown, unavailability
from devloop.dispatcher import Dispatcher
from devloop.planning import Action, Snapshot
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeWS, pr
from test_devloop_agy_permissions import (DENIED, DENIED_ENVELOPE, JETSKI, NO_FALLBACK,
                                          SequenceCLI, block_commands)
from test_devloop_fallback import ARGON, HYDROGEN, QUOTA, FakeCLI, with_agent
from test_devloop_fallback_memory import T0, Clock

HELIUM = 'gemini-4-helium-high'
# A denied action whose arguments carry the command line, as a later agy may report it.
COMMAND_ENVELOPE = json.dumps({'status': 'SUCCESS', 'response': '', 'denied_actions': [
    {'action': 'command', 'display_name': 'Bash',
     'args': {'command': 'grep -rn denied /home/op/ssdlc/devloop'}}]})
COMMAND = 'grep -rn denied …/devloop'  # its absolute path reduced to the last segment
NOTE = '\n\n## Denied in the previous attempt\n'


def two_fallbacks(**table):
    """agy with Hydrogen and then Helium as fallback models."""
    return config.parse_config(with_agent('agy', fallback_models=[
        {'model': HYDROGEN, 'model_label': 'Gemini 4 Hydrogen'},
        {'model': HELIUM, 'model_label': 'Gemini 4 Helium'}], **table))


def retry_note(prompt):
    """The ``## Denied in the previous attempt`` block of ``prompt``, up to the command block."""
    start = prompt.index(NOTE)
    return prompt[start:prompt.index('\n\n## Shell commands\n', start)]


class DeniedCommandTests(unittest.TestCase):
    def test_the_command_is_read_from_the_last_denied_action(self):
        self.assertEqual(agents.denied_command('agy', COMMAND_ENVELOPE), COMMAND)
        listed = json.dumps({'denied_actions': [{'action': 'command', 'command': 'ls'},
                                                {'action': 'command',
                                                 'command_line': ['cat', 'x.log']}]})
        self.assertEqual(agents.denied_command('agy', listed), 'cat x.log')
        # agy 1.5 reports no command line: nothing is made up.
        for out in (DENIED_ENVELOPE, '{"denied_actions":[]}', 'not json', '[1]', ''):
            self.assertIsNone(agents.denied_command('agy', out))
        self.assertIsNone(agents.denied_command('claude', COMMAND_ENVELOPE))

    def test_the_runner_attaches_it_to_the_denial(self):
        for out, command in ((COMMAND_ENVELOPE, COMMAND), (DENIED_ENVELOPE, None)):
            cli = FakeCLI(gemini_4_argon_high=(0, out, JETSKI))
            with tempfile.TemporaryDirectory() as tmp, \
                    self.assertRaises(agents.PermissionDenied) as ctx:
                agents.AgentRunner(tmp, 10, run=cli).run(CFG.agent('agy'), 'review', 'p',
                                                         'review', tmp, 'pr-1')
            self.assertEqual((ctx.exception.permission, ctx.exception.command),
                             ('command', command))
        self.assertEqual(ctx.exception.denied(),
                         f'comando non riportato da agy (action: {DENIED[1:-1]})')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def memory(self):
        return unavailability.Memory(self.state, clock=self.clock)

    def review(self, cli, cfg=CFG):
        """One tick reviewing a PR whose reviewer is agy, by a new dispatcher (a restart).
        Returns the first comment, the review or the escalation."""
        self.gh, self.logs = FakeGH(), []
        runner = agents.AgentRunner(Path(self.tmp.name) / 'logs', 10, run=cli, clock=self.clock)
        d = Dispatcher(cfg, self.gh, FakeWS(), runner, log=self.logs.append,
                       memory=self.memory())
        p = pr()
        p.meta['reviewer'] = 'agy'
        self.gh.labels[100] = set(p.labels)
        d.snapshot = lambda: Snapshot([p], [], [])
        self.assertEqual(d.tick(), [Action('review', 100)])
        return self.gh.comments[0][1]


class RetryPromptTests(Base):
    """The retry is told what was denied and which prefixes it may run (#319)."""

    def test_the_retry_names_the_denied_command_and_the_allowed_prefixes(self):
        cli = SequenceCLI(gemini_4_argon_high=[(0, COMMAND_ENVELOPE, JETSKI)])
        self.review(cli)
        self.assertEqual(cli.models(), [ARGON, ARGON])
        self.assertNotIn(NOTE, cli.calls[0][1])
        note = retry_note(cli.calls[1][1])
        self.assertIn(f'headless mode denied the `command` permission (command `{COMMAND}`)',
                      note)
        self.assertIn('Do not run it again. Use only shell commands that start with one of '
                      'these prefixes:', note)
        allowed = list(CFG.agent('agy').review_commands)
        self.assertEqual([line[3:-1] for line in note.splitlines() if line.startswith('- `')],
                         allowed)
        self.assertEqual(block_commands(cli.calls[1][1]), allowed)  # the same list as --settings
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})

    def test_without_the_command_the_retry_says_so_and_still_lists_the_prefixes(self):
        cli = SequenceCLI(gemini_4_argon_high=[(0, DENIED_ENVELOPE, JETSKI)])
        self.review(cli)
        note = retry_note(cli.calls[1][1])
        self.assertIn('denied the `command` permission (comando non riportato da agy (action: '
                      f'{DENIED[1:-1]}))', note)
        self.assertIn('- `python3 -m unittest`\n', note)

    def test_a_mode_without_commands_says_none_may_run(self):
        agent = config.parse_config(with_agent('agy', review_commands=[])).agent('agy')
        denial = agents.PermissionDenied('x', permission='command')
        note = agents.denied_retry_note(agent, 'review', denial)
        self.assertIn('Run no shell command in this turn.', note)
        self.assertNotIn('- `', note)


class NextFallbackTests(Base):
    """A model denied twice leaves the turn to the next fallback, and is remembered (#319)."""

    def test_the_configured_cooldown_defaults_to_six_hours_and_is_validated(self):
        self.assertEqual(CFG.agent('agy').denied_cooldown_hours, 6)
        self.assertEqual(two_fallbacks(denied_cooldown_hours=2).agent('agy')
                         .denied_cooldown_hours, 2)
        for value in (0, -1, '6', True, 1.5):
            with self.subTest(value), self.assertRaises(config.ConfigError):
                config.parse_config(with_agent('agy', denied_cooldown_hours=value))
        self.assertIn('denied_cooldown_hours', config.OVERLAY_KEYS['agents']['*'])
        self.assertIn('# denied_cooldown_hours = 6', config.DEFAULT_CONFIG.read_text())

    def test_a_second_denial_moves_to_the_next_fallback_and_remembers_the_model(self):
        cfg = two_fallbacks(denied_cooldown_hours=2)
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA),
                      gemini_4_hydrogen_high=(0, COMMAND_ENVELOPE, JETSKI))
        body = self.review(cli, cfg)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN, HYDROGEN, HELIUM])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(markdown.read_marker(body, 'review')['profile']['model'], HELIUM)
        self.assertIn('Previous attempt on another model', cli.calls[3][1])
        self.assertNotIn(NOTE, cli.calls[3][1])  # the note is for the denied model only
        until = unavailability.iso(T0 + 2 * 3600)
        log = '\n'.join(self.logs)
        self.assertIn(f'agy: model {HYDROGEN} denied twice; trying {HELIUM}; skipped until '
                      f'{until}', log)
        entry = self.memory().active('agy', HYDROGEN)
        self.assertEqual(entry['until'], until)
        self.assertEqual(entry['reason'],
                         f'permission `command` denied twice (command `{COMMAND}`)')

        # Within the period the next turns skip it, without running it.
        self.clock.now = T0 + 1800
        cli = FakeCLI()
        record = markdown.read_marker(self.review(cli, cfg), 'review')
        self.assertEqual(cli.models(), [HELIUM])
        self.assertIn(f'agy: model {HYDROGEN} skipped: unavailable until {until} (permission '
                      '`command` denied twice', '\n'.join(self.logs))
        self.assertEqual([s['model'] for s in record['skipped_models']], [ARGON, HYDROGEN])

        # Once it has passed, the model is tried again.
        self.clock.now = T0 + 2 * 3600
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA))
        self.review(cli, cfg)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN])
        self.assertIsNone(self.memory().active('agy', HYDROGEN))

    def test_the_configured_model_denied_twice_moves_to_its_fallback(self):
        cli = FakeCLI(gemini_4_argon_high=(0, DENIED_ENVELOPE, JETSKI))
        self.review(cli)  # the default configuration: six hours
        self.assertEqual(cli.models(), [ARGON, ARGON, HYDROGEN])
        self.assertEqual(self.gh.labels[100], {'agent:ready-to-merge'})
        self.assertEqual(self.memory().active('agy', ARGON)['until'],
                         unavailability.iso(T0 + 6 * 3600))


class EscalationTests(Base):
    """Without another model the escalation stays a ``tool_failure``, naming the command and the
    model (#319)."""

    def test_the_last_fallback_escalates_naming_the_model_and_the_command(self):
        cli = FakeCLI(gemini_4_argon_high=(1, '', QUOTA),
                      gemini_4_hydrogen_high=(0, COMMAND_ENVELOPE, JETSKI))
        body = self.review(cli)
        self.assertEqual(cli.models(), [ARGON, HYDROGEN, HYDROGEN])
        self.assertEqual(self.gh.labels[100], {'agent:needs-operator'})
        self.assertEqual(markdown.read_marker(body, 'notice')['class'], 'tool_failure')
        self.assertIn('the last model tried for `agy` failed', body)
        self.assertIn(f'permission `command` denied again after one retry; model `{HYDROGEN}`; '
                      f'command `{COMMAND}`', body)
        # The last model is not remembered: nothing would be left to run.
        self.assertIsNone(self.memory().active('agy', HYDROGEN))

    def test_without_the_command_the_escalation_says_so(self):
        cli = FakeCLI(gemini_4_argon_high=(0, DENIED_ENVELOPE, JETSKI))
        body = self.review(cli, NO_FALLBACK)
        self.assertEqual(cli.models(), [ARGON, ARGON])
        self.assertEqual(markdown.read_marker(body, 'notice')['class'], 'tool_failure')
        self.assertIn(f'permission `command` denied again after one retry; model `{ARGON}`; '
                      f'comando non riportato da agy (action: {DENIED[1:-1]})', body)


if __name__ == '__main__':
    unittest.main()
