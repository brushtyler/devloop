"""devloop: the operator's direction of a project (#604, ADR 0002, second precisazione of
2026-11-23, point 7): ``[direction] issue`` of the overlay, ``devloop direction`` and its line in
``devloop status``. The latest comment of a trusted user that starts with the line
``## Direzione`` is the direction in force, given back as it is; it never reaches a prompt."""

import ast
import contextlib
import dataclasses
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import devloop.__main__ as cli  # noqa: E402
from devloop import config, direction  # noqa: E402
from devloop.config import ConfigError, DirectionPolicy  # noqa: E402
from devloop.context import PROMPT_DIR  # noqa: E402
from devloop.dispatcher import Dispatcher  # noqa: E402
from devloop.github import GitHubError  # noqa: E402

from helpers import ssdlc_project  # noqa: E402
from helpers.devloop_fakes import FakeGH  # noqa: E402
from test_devloop import CFG, FakeRunner, FakeWS, implemented  # noqa: E402

ISSUE = 604
OPERATOR = CFG.trusted_users[0]
APP = 'brushtyler-devloop-coordinator[bot]'
FIRST = '## Direzione\n1. Direzione — tema uno; issue: #10\nIn pausa: #20\nVincoli: budget\n'
SECOND = '## Direzione\n1. Tema due — fatto quando #30 è chiusa\nVincoli: nessuno\n'
PACKAGE = Path(direction.__file__).resolve().parent


def comment(login, body, created, updated=None, number=1):
    return {'id': number, 'author': {'login': login}, 'body': body, 'created_at': created,
            'updated_at': updated or created,
            'url': f'https://github.com/o/r/issues/{ISSUE}#issuecomment-{number}'}


def with_issue(cfg=CFG, issue=ISSUE):
    return dataclasses.replace(cfg, direction=DirectionPolicy(issue=issue))


class Comments:
    """The comments of the direction issue, and every issue whose comments were read."""

    def __init__(self, comments, fail=None):
        self.comments, self.fail, self.read = list(comments), fail, []

    def issue_comments(self, number):
        self.read.append(number)
        if self.fail:
            raise self.fail
        return list(self.comments) if number == ISSUE else []


class FindTests(unittest.TestCase):
    def find(self, comments, logs=None):
        return direction.find(ISSUE, comments, CFG.trusted_users,
                              log=(logs.append if logs is not None else lambda line: None))

    def test_the_latest_valid_comment_is_the_direction(self):
        found = self.find([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z', number=1),
                           comment(OPERATOR, SECOND, '2026-11-23T10:00:00Z', number=2)])
        self.assertEqual(found.body, SECOND)  # as written, not interpreted
        self.assertEqual((found.issue, found.author, found.created_at),
                         (ISSUE, OPERATOR, '2026-11-23T10:00:00Z'))
        self.assertTrue(found.url.endswith('#issuecomment-2'))
        self.assertEqual(found.header(),
                         f'direction: issue #{ISSUE}, comment of {OPERATOR} at '
                         f'2026-11-23T10:00:00Z: {found.url}')

    def test_a_discussion_after_a_direction_changes_nothing(self):
        found = self.find([
            comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z', number=1),
            comment(OPERATOR, 'Una domanda: #20 resta in pausa anche la prossima settimana?',
                    '2026-11-21T10:00:00Z', number=2),
            comment(APP, 'Proposta del coordinatore: alzare #10 a `high`.',
                    '2026-11-22T10:00:00Z', number=3)])
        self.assertEqual(found.body, FIRST)
        self.assertTrue(found.url.endswith('#issuecomment-1'))

    def test_the_comment_of_an_app_never_counts(self):
        # Even when its login is among trusted_users, and with the heading.
        found = self.find([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z', number=1),
                           comment(APP, SECOND, '2026-11-23T10:00:00Z', number=2)])
        self.assertEqual(found.body, FIRST)
        self.assertIsNone(direction.find(ISSUE, [comment(APP, SECOND, '2026-11-23T10:00:00Z')],
                                         (*CFG.trusted_users, APP)))

    def test_no_valid_comment(self):
        logs = []
        self.assertIsNone(self.find([], logs))
        self.assertIsNone(self.find([
            comment('stranger', SECOND, '2026-11-20T10:00:00Z', number=1),  # not trusted
            comment(OPERATOR, 'Riprendiamo la discussione.', '2026-11-21T10:00:00Z', number=2),
            comment(OPERATOR, f'Intro\n\n{SECOND}', '2026-11-22T10:00:00Z', number=3),
            comment(OPERATOR, '### Direzione\n1. tema', '2026-11-23T10:00:00Z', number=4),
            comment(OPERATOR, '## direzione\n1. tema', '2026-11-23T11:00:00Z', number=5)],
            logs))
        # The comments of a trusted user that look like a direction are logged, not used.
        self.assertEqual(len(logs), 3, logs)
        for line, number in zip(logs, (3, 4, 5)):
            self.assertIn(f'#issuecomment-{number})', line)
            self.assertIn('looks like a direction but does not start with the line '
                          '`## Direzione`: not used', line)

    def test_the_heading_may_follow_blank_lines_and_have_trailing_spaces(self):
        body = '\n\n## Direzione  \n1. tema\n'
        self.assertEqual(self.find([comment(OPERATOR, body, '2026-11-20T10:00:00Z')]).body,
                         body)

    def test_an_old_malformed_comment_is_not_logged(self):
        logs = []
        found = self.find([comment(OPERATOR, 'Direzione:\n1. tema', '2026-11-19T10:00:00Z',
                                   number=1),
                           comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z', number=2)], logs)
        self.assertEqual(found.body, FIRST)
        self.assertEqual(logs, [])

    def test_an_edit_is_shown(self):
        found = self.find([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z',
                                   '2026-11-21T09:00:00Z')])
        self.assertIn('at 2026-11-20T10:00:00Z, edited 2026-11-21T09:00:00Z: ', found.header())


class ReadTests(unittest.TestCase):
    def test_issue_not_configured(self):
        gh = Comments([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z')])
        self.assertIsNone(direction.read(CFG, gh))
        self.assertEqual(gh.read, [])  # nothing read on GitHub
        self.assertIn('no [direction] issue is configured', direction.missing(CFG))

    def test_configured(self):
        gh = Comments([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z')])
        self.assertEqual(direction.read(with_issue(), gh).body, FIRST)
        self.assertEqual(gh.read, [ISSUE])
        self.assertIn(f'no comment of a trusted user on issue #{ISSUE} starts with the line '
                      '`## Direzione`', direction.missing(with_issue()))


class CommandTests(unittest.TestCase):
    def run_cli(self, cfg, gh):
        identities = SimpleNamespace(github=lambda actor: gh)
        out, logs = io.StringIO(), []
        with mock.patch.object(cli, 'load_config', return_value=cfg), \
                mock.patch.object(cli.Identities, 'from_config', return_value=identities), \
                mock.patch.object(cli, 'log', logs.append), contextlib.redirect_stdout(out):
            cli.main(['direction'])
        return out.getvalue(), logs

    def test_prints_the_direction_as_it_is(self):
        gh = Comments([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z', number=1),
                       comment(OPERATOR, 'Direzione: rivedere #20', '2026-11-21T10:00:00Z',
                               number=2)])
        out, logs = self.run_cli(with_issue(), gh)
        header, body = out.split('\n', 1)
        self.assertEqual(header, f'direction: issue #{ISSUE}, comment of {OPERATOR} at '
                                 f'2026-11-20T10:00:00Z: https://github.com/o/r/issues/{ISSUE}'
                                 '#issuecomment-1')
        self.assertEqual(body, FIRST)
        self.assertEqual(len(logs), 1)
        self.assertIn('#issuecomment-2) looks like a direction', logs[0])

    def test_issue_not_configured(self):
        gh = Comments([])
        out, _ = self.run_cli(CFG, gh)
        self.assertTrue(out.startswith('direction: none: no [direction] issue is configured'))
        self.assertEqual(gh.read, [])

    def test_no_valid_comment(self):
        out, _ = self.run_cli(with_issue(), Comments([comment(APP, FIRST, '2026-11-20T10:00:00Z')]))
        self.assertTrue(out.startswith(f'direction: none: no comment of a trusted user on issue '
                                       f'#{ISSUE}'), out)

    def test_a_failed_read_exits_with_its_message(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli(with_issue(), Comments([], fail=GitHubError('HTTP 502')))
        self.assertEqual(raised.exception.code, 'devloop: direction: HTTP 502')


class StatusLineTests(unittest.TestCase):
    def test_the_line_of_status(self):
        self.assertIn('no [direction] issue is configured', cli.direction_line(CFG, None))
        gh = Comments([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z')])
        self.assertEqual(cli.direction_line(with_issue(), gh),
                         f'direction: issue #{ISSUE}, comment of {OPERATOR} at '
                         f'2026-11-20T10:00:00Z: https://github.com/o/r/issues/{ISSUE}'
                         '#issuecomment-1')
        self.assertIn('no comment of a trusted user',
                      cli.direction_line(with_issue(), Comments([])))
        self.assertEqual(cli.direction_line(with_issue(), Comments([], GitHubError('HTTP 502'))),
                         f'direction: not read from issue #{ISSUE}: HTTP 502')

    def test_status_shows_it(self):
        lines = []
        d = SimpleNamespace(gh=Comments([comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z')]),
                            pinner=None)
        with mock.patch.object(cli, 'build', return_value=(with_issue(), Path('/state'), d)), \
                mock.patch.object(cli, 'log', lines.append), \
                mock.patch.object(cli, 'label_transition', side_effect=RuntimeError('stop')), \
                self.assertRaises(RuntimeError):
            cli.main(['status'])
        self.assertIn(f'direction: issue #{ISSUE}, comment of {OPERATOR} at '
                      f'2026-11-20T10:00:00Z: https://github.com/o/r/issues/{ISSUE}'
                      '#issuecomment-1', lines)


class ConfigTests(unittest.TestCase):
    def test_default_and_values(self):
        self.assertEqual(CFG.direction, DirectionPolicy(issue=0))
        self.assertEqual(config.parse_direction({}), DirectionPolicy())
        self.assertEqual(config.parse_direction({'issue': 604}), DirectionPolicy(issue=604))
        for table in ({'issue': 0}, {'issue': True}, {'issue': '5'}, {'issue': 1.5},
                      {'other': 1}, []):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                config.parse_direction(table)

    def test_the_overlay_sets_it(self):
        data = config.apply_overlay(ssdlc_project.data(), {'direction': {'issue': 604}})
        self.assertEqual(config.parse_config(data).direction.issue, 604)
        with self.assertRaises(ConfigError):
            config.apply_overlay(ssdlc_project.data(), {'direction': {'other': 1}})

    def test_not_a_key_of_the_project_file(self):
        self.assertNotIn('direction', config.PROJECT_KEYS)


class DirectionGH(FakeGH):
    """FakeGH whose direction issue has a direction, recording every issue whose comments are
    read."""

    def __init__(self):
        super().__init__()
        self.read = []

    def issue_comments(self, number):
        self.read.append(number)
        if number == ISSUE:
            return [comment(OPERATOR, FIRST, '2026-11-20T10:00:00Z')]
        return super().issue_comments(number)


class NotInThePromptsTests(unittest.TestCase):
    def test_the_implementer_never_sees_it(self):
        gh, runner = DirectionGH(), FakeRunner(implemented())
        Dispatcher(with_issue(), gh, FakeWS(), runner, log=lambda m: None)._do_implement(7)
        prompt = runner.calls[0][3]
        self.assertIn('Also X.', prompt)  # the trusted comments of the issue are there
        self.assertNotIn('tema uno', prompt)
        self.assertNotIn('## Direzione', prompt)
        self.assertNotIn(ISSUE, gh.read)

    def test_only_the_commands_read_it(self):
        # No module of the dispatcher, of the prompts or of the agents imports devloop.direction:
        # only the command line (devloop direction, devloop status) does.
        importers = set()
        for path in PACKAGE.glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names]
                         if isinstance(node, (ast.Import, ast.ImportFrom)) else [])
                module = getattr(node, 'module', None) or ''
                if 'direction' in names or module.endswith('direction'):
                    importers.add(path.name)
        self.assertEqual(importers, {'__main__.py'})
        for template in PROMPT_DIR.glob('*.md'):
            with self.subTest(template=template.name):
                text = template.read_text(encoding='utf-8')
                self.assertNotIn('direction', text)
                self.assertNotIn('Direzione', text)


if __name__ == '__main__':
    unittest.main()
