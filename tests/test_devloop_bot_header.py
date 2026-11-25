"""devloop: the one attribution block of the texts devloop publishes, Bot and Version first,
and the old headers still read (#232)."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from devloop import markdown
from devloop.agents import AgentRunner
from devloop.apps import DISPATCHER
from devloop.context import operator_comments
from devloop.dispatch_supervision import fix_command, merge_command
from devloop.dispatcher import Dispatcher, preflight_hash
from devloop.supervision import supervisor_command
from helpers.devloop_fakes import FakeGH
from test_devloop import CFG, FakeRunner, FakeWS, pr

COMMIT = '0123456789abcdef0123456789abcdef01234567'
# The header of the dispatcher's texts before #232: records published with it stay valid.
OLD_HEADER = ('> [!NOTE]\n'
              '> **Agent:** devloop dispatcher\n'
              '> **Model:** nessuno (testo generato dal dispatcher)\n'
              '> **Reasoning Level:** n/a\n')
NEW_HEADER = markdown.bot_header(markdown.DISPATCHER, COMMIT[:12])
HEADERS = {'old': OLD_HEADER, 'new': NEW_HEADER}
# The texts of an AI agent devloop publishes: before #232 the agent header alone, since then one
# block with Bot and Version followed by Agent, Model and Reasoning Level.
OLD_AGENT_HEADER = markdown.header('Codex', '0.5', 'GPT', 'high')
NEW_AGENT_HEADER = markdown.header('Codex', '0.5', 'GPT', 'high',
                                   bot=markdown.agent_bot(CFG.implementer), version=COMMIT[:12])
ALL_HEADERS = dict(HEADERS, old_agent=OLD_AGENT_HEADER, new_agent=NEW_AGENT_HEADER)
DISPATCHER_LOGIN = 'brushtyler-devloop-dispatcher[bot]'
OPERATOR = 'brushtyler'


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


def closed_finding(header):
    """A record of findings closed by the operator, with ``header``."""
    payload = {'closed': [{'key': '1/codex/F1', 'status': 'resolved', 'by': 'operator',
                           'author': OPERATOR, 'reason': 'corretto', 'head_sha': COMMIT}]}
    return markdown.publishable(header + '\n## Finding chiusi\n\n- `F1`', 'finding', payload)


def notice(header):
    return markdown.publishable(header + '\n## Avviso\n\nx', 'notice',
                                {'event': 'escalation', 'class': 'failure', 'critical': True,
                                 'to': 'operator'})


class AppIdentities:
    """Every actor of devloop, the dispatcher included, has its own GitHub App."""

    def __init__(self, gh):
        self.gh = gh

    def login(self, actor):
        return DISPATCHER_LOGIN if actor == DISPATCHER else f'{actor}-agent[bot]'

    def agent_logins(self):
        return {self.login(actor) for actor in CFG.agents}

    def github(self, actor, permissions=None):
        return self.gh


def comment(login, body):
    return {'author': {'login': login}, 'body': body}


class HeaderTests(unittest.TestCase):
    def test_the_dispatcher_header_names_the_bot_and_the_pinned_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'pin.json').write_text(json.dumps({'commit': COMMIT, 'files': []}))
            self.assertEqual(markdown.dispatcher_header(tmp),
                             '> [!NOTE]\n> **Bot:** devloop dispatcher\n'
                             f'> **Version:** {COMMIT[:12]}\n')
            self.assertEqual(markdown.devloop_version(tmp), '0123456789ab')

    def test_without_a_pin_the_version_is_the_checkout_or_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, 'repo')
            repo.mkdir()
            git(repo, 'init', '-q')
            git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                'commit', '-q', '--allow-empty', '-m', 'x')
            head = git(repo, 'rev-parse', 'HEAD')
            self.assertEqual(markdown.devloop_version(str(repo)), head[:12])
            self.assertTrue(markdown.dispatcher_header(str(repo)).endswith(
                f'> **Version:** {head[:12]}\n'))
            # A directory inside a checkout, such as a pinned copy that lost its stamp, is not
            # that checkout: its commit is not the version of devloop.
            inner = repo / 'pinned-x'
            inner.mkdir()
            self.assertEqual(markdown.devloop_version(str(inner)), 'non nota')
            # Neither a pin nor a checkout; a malformed stamp counts as none.
            bare = Path(tmp, 'bare')
            bare.mkdir()
            Path(bare, 'pin.json').write_text(json.dumps({'commit': 'main'}))
            self.assertEqual(markdown.dispatcher_header(str(bare)),
                             '> [!NOTE]\n> **Bot:** devloop dispatcher\n'
                             '> **Version:** non nota\n')

    def test_texts_the_dispatcher_writes_carry_the_bot_header(self):
        body = markdown.render_escalation('Motivo.')
        lines = body.splitlines()
        self.assertEqual(lines[:2], ['> [!NOTE]', '> **Bot:** devloop dispatcher'])
        self.assertTrue(re.fullmatch(r'> \*\*Version:\*\* ([0-9a-f]{12}|non nota)', lines[2]))
        self.assertNotIn('**Agent:**', body)
        self.assertNotIn('**Model:**', body)

    def test_the_texts_of_ai_agents_carry_bot_and_version_in_the_same_block(self):
        self.assertEqual(markdown.header('Codex', '0.5.1', 'GPT-5.5', 'high',
                                         bot='devloop codex-agent', version=COMMIT[:12]),
                         '> [!NOTE]\n> **Bot:** devloop codex-agent\n'
                         f'> **Version:** {COMMIT[:12]}\n'
                         '> **Agent:** Codex (CLI v0.5.1)\n'
                         '> **Model:** GPT-5.5\n> **Reasoning Level:** High\n')
        self.assertEqual([markdown.agent_bot(k) for k in ('claude', 'codex', 'agy')],
                         ['devloop claude-agent', 'devloop codex-agent', 'devloop agy-agent'])

    def test_the_runner_composes_one_block_for_the_texts_of_an_agent(self):
        def fake_run(argv, **kw):
            return SimpleNamespace(returncode=0, stdout='2.1.322 (Claude Code)', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            agent = CFG.agent('claude')
            head = AgentRunner(tmp, 10, run=fake_run).header(agent)
        lines = head.splitlines()
        self.assertEqual(lines[:2], ['> [!NOTE]', '> **Bot:** devloop claude-agent'])
        self.assertTrue(re.fullmatch(r'> \*\*Version:\*\* ([0-9a-f]{12}|non nota)', lines[2]))
        self.assertEqual(lines[3:], [f'> **Agent:** {agent.display_name} (CLI v2.1.322)',
                                     f'> **Model:** {agent.model_label}',
                                     f'> **Reasoning Level:** '
                                     f'{markdown.effort_label(agent.effort)}'])
        self.assertEqual(head.count('[!NOTE]'), 1)
        self.assertEqual(lines[2], f'> **Version:** {markdown.devloop_version()}')

    def test_outside_devloop_an_agent_header_has_no_bot_lines(self):
        self.assertEqual(markdown.header('Claude Code', '2.1.322', 'Claude Opus 5.5', 'high'),
                         '> [!NOTE]\n> **Agent:** Claude Code (CLI v2.1.322)\n'
                         '> **Model:** Claude Opus 5.5\n> **Reasoning Level:** High\n')


class RecognitionTests(unittest.TestCase):
    def make(self, apps):
        self.gh = FakeGH()
        return Dispatcher(CFG, self.gh, FakeWS(), FakeRunner(), log=lambda *_: None,
                          identities=AppIdentities(self.gh) if apps else None)

    def test_records_with_either_header_from_the_dispatcher_app(self):
        d = self.make(apps=True)
        for name, header in HEADERS.items():
            with self.subTest(header=name):
                mine = comment(DISPATCHER_LOGIN, notice(header))
                self.assertTrue(d._automatic(mine))
                entries = d._marker_entries(pr(), [comment(DISPATCHER_LOGIN,
                                                           closed_finding(header))], 'finding')
                self.assertEqual(len(entries), 1)

    def test_a_bot_header_from_another_identity_is_no_record(self):
        d = self.make(apps=True)
        for name, header in HEADERS.items():
            for login in ('stranger', OPERATOR):
                with self.subTest(header=name, login=login):
                    self.assertFalse(d._automatic(comment(login, notice(header))))
                    self.assertEqual(d._marker_entries(
                        pr(), [comment(login, closed_finding(header))], 'finding'), [])

    def test_agent_records_with_either_header_only_from_the_agent_app(self):
        d = self.make(apps=True)
        fix = {'round': 1, 'head_sha': COMMIT, 'responses': []}
        for name, header in (('old', OLD_AGENT_HEADER), ('new', NEW_AGENT_HEADER)):
            body = markdown.publishable(header + '\n## Correzioni\n\nx', 'fix', fix)
            with self.subTest(header=name):
                implementer = f'{CFG.implementer}-agent[bot]'
                self.assertEqual(len(d._marker_entries(pr(), [comment(implementer, body)],
                                                       'fix')), 1)
                for login in ('stranger', OPERATOR, DISPATCHER_LOGIN):
                    self.assertEqual(d._marker_entries(pr(), [comment(login, body)], 'fix'), [])

    def test_without_apps_both_headers_from_a_trusted_login_are_records(self):
        d = self.make(apps=False)
        for name, header in HEADERS.items():
            with self.subTest(header=name):
                self.assertTrue(d._automatic(comment(OPERATOR, notice(header))))
                self.assertFalse(d._automatic(comment('stranger', notice(header))))
                self.assertEqual(len(d._marker_entries(
                    pr(), [comment(OPERATOR, closed_finding(header))], 'finding')), 1)

    def test_the_readiness_hash_leaves_out_records_with_either_header(self):
        d = self.make(apps=False)

        def h(*comments):
            return preflight_hash('t', 'b', list(comments), CFG.trusted_users, d._automatic)
        base = h(comment(OPERATOR, 'Decido A.'))
        for name, header in HEADERS.items():
            with self.subTest(header=name):
                self.assertEqual(base, h(comment(OPERATOR, 'Decido A.'),
                                         comment(OPERATOR, notice(header))))


class OperatorTextTests(unittest.TestCase):
    def test_operator_comments_leave_out_texts_with_either_header(self):
        comments = [comment(OPERATOR, 'Va bene così.'),
                    comment(OPERATOR, OLD_HEADER + '\n## Escalation\n\nx'),
                    comment(OPERATOR, NEW_HEADER + '\n## Escalation\n\nx'),
                    comment(OPERATOR, markdown.dispatcher_header() + '\nx'),
                    comment(OPERATOR, OLD_AGENT_HEADER + '\n## Review\n\nx'),
                    comment(OPERATOR, NEW_AGENT_HEADER + '\n## Review\n\nx'),
                    comment('stranger', 'Anche io.')]
        self.assertEqual(operator_comments(comments, CFG.trusted_users), [comments[0]])

    def test_commands_after_either_header(self):
        for name, header in ALL_HEADERS.items():
            with self.subTest(header=name):
                self.assertEqual(supervisor_command(header + '\n/devloop retry'), ('retry', ''))
                self.assertEqual(supervisor_command(header + '\n/devloop escalate perché'),
                                 ('escalate', 'perché'))
                self.assertEqual(merge_command(header + '\n/devloop hold motivo'),
                                 ('hold', 'motivo'))
                self.assertEqual(merge_command(header + '\n/devloop release'), ('release', ''))
                self.assertEqual(fix_command(header + '\n/devloop fix rinomina x\ne y'),
                                 'rinomina x\ne y')
                self.assertIsNone(supervisor_command(header + '\nTesto.'))


class DocumentationTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_agents_md_holds_only_the_directive_with_a_link_to_the_format(self):
        text = (self.ROOT / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertIn('Nei turni di devloop gli agent non aggiungono nessuna intestazione', text)
        self.assertIn('(docs/devloop/protocol.md#attribution)', text)
        # The format of devloop's header is documented in docs/devloop/protocol.md, not in AGENTS.md.
        for absent in ('**Bot:**', '**Version:**', 'Testi pubblicati da devloop'):
            self.assertNotIn(absent, text)

    def test_agents_md_trusts_the_role_apps_as_provenance_only(self):
        text = (self.ROOT / 'AGENTS.md').read_text(encoding='utf-8')
        section = text.split('## Contenuti di autori non fidati', 1)[1].split('\n## ', 1)[0]
        # The role apps are trusted sources (#649), but their texts are not the operator's requests.
        self.assertIn('le app di ruolo del progetto (`supervisor`, `coordinator`, `implementer`', section)
        self.assertIn('fidate come **provenienza**', section)
        self.assertIn("Un'issue aperta dal coordinatore resta una proposta", section)

    def test_the_devloop_document_describes_the_format(self):
        text = (self.ROOT / 'docs' / 'devloop' / 'protocol.md').read_text(encoding='utf-8')
        section = text[text.index('\n## Attribution\n'):]
        section = section[:section.index('\n## ', 1)]
        for line in ('> **Bot:** devloop dispatcher', '> **Version:** 0123456789ab',
                     '> **Bot:** devloop codex-agent', '> **Agent:** devloop dispatcher',
                     '`pin.json`', '`operator_comments`'):
            self.assertIn(line, section)


if __name__ == '__main__':
    unittest.main()
