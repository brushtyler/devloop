"""The host's details out of the published texts (#583, ``devloop/redact.py``).

The substitution of the paths, users and key files with the names of the parameters, longest
path first; the map of ops with the runtime values of the dispatcher prevailing; the clients of
``apps.Identities`` that redact every write before the outbox; and the last check of the role
scripts (``scripts/supervisor-token.py``, ``scripts/coordinator-token.py``), which refuse a text
with a path of the host unless ``--allow-host-details``, logged.
"""

import importlib.util
import io
import json
import os
import pwd
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devloop import apps, conclave, ledger, markdown, redact
from helpers import ssdlc_project

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'acme-widget-'

# The map ops/host/setup-devloop.sh writes, with values that are not the defaults.
MAP = {
    'paths': {
        'STATE_DIR': '/srv/u-dl/clone/.state',
        'DEVLOOP_KEY_DIR': '/srv/u-dl/.keys',
        'SUPERVISOR_KEY_DIR': '/srv/u-sv/.keys-widget',
        'DEVLOOP_HOME': '/srv/u-dl',
        'AGENT_HOME': '/srv/u-ag',
        'SUPERVISOR_HOME': '/srv/u-sv',
        'COORDINATION_DIR': '/var/lib/box-widget',
    },
    'users': {'DEVLOOP_USER': 'u-dl', 'AGENT_USER': 'u-ag', 'SUPERVISOR_USER': 'u-sv',
              'COORDINATOR_USER': 'coordinator'},
    'app_prefix': PREFIX,
}


def redactor(**kw):
    return redact.Redactor.from_map(MAP, **kw)


class RedactorTests(unittest.TestCase):
    def test_the_longest_path_first(self):
        # The state_dir lies in devloop's home: $STATE_DIR, never $DEVLOOP_HOME/clone/.state.
        r = redactor()
        self.assertEqual(r.redact('cd /srv/u-dl/clone/.state/worktrees/issue-570 && ls'),
                         'cd $STATE_DIR/worktrees/issue-570 && ls')
        self.assertEqual(r.redact('/srv/u-dl/clone/.git'), '$DEVLOOP_HOME/clone/.git')
        self.assertEqual(r.redact('/srv/u-sv/.keys-widget/x'), '$SUPERVISOR_KEY_DIR/x')
        self.assertEqual(r.redact('/var/lib/box-widget'), '$COORDINATION_DIR')

    def test_a_path_ends_where_a_name_does(self):
        r = redactor()
        # Another directory whose name starts as a home's: not that home.
        self.assertEqual(r.redact('/srv/u-dl2/x /srv/u-dl.old /srv/u-dl-agent'),
                         '/srv/u-dl2/x /srv/u-dl.old /srv/u-dl-agent')
        self.assertEqual(r.redact('"/srv/u-dl", (/srv/u-dl/).'),
                         '"$DEVLOOP_HOME", ($DEVLOOP_HOME/).')

    def test_several_users_in_the_same_text(self):
        # A traceback holds the homes of the dispatcher and of the agent user: $HOME alone
        # would be ambiguous.
        text = ('Traceback (most recent call last):\n'
                '  File "/srv/u-dl/clone/.state/worktrees/pr-12/tests/test_x.py", line 4\n'
                "PermissionError: [Errno 13] '/srv/u-ag/.codex/config.toml'\n"
                'sudo -n -E -u u-ag -- bash (as u-dl), owner u-sv\n')
        self.assertEqual(redactor().redact(text), (
            'Traceback (most recent call last):\n'
            '  File "$STATE_DIR/worktrees/pr-12/tests/test_x.py", line 4\n'
            "PermissionError: [Errno 13] '$AGENT_HOME/.codex/config.toml'\n"
            'sudo -n -E -u <AGENT_USER> -- bash (as <DEVLOOP_USER>), owner <SUPERVISOR_USER>\n'))

    def test_users_only_alone_and_never_the_documented_defaults(self):
        r = redactor()
        # coordinator is a default name, and a word of devloop's own texts.
        self.assertEqual(r.redact('the coordinator and devloop'), 'the coordinator and devloop')
        self.assertEqual(r.redact('u-dl-x u-dl_x xu-dl /x/u-dl u-dl.'),
                         'u-dl-x u-dl_x xu-dl /x/u-dl <DEVLOOP_USER>.')
        defaults = redact.Redactor(users=[('DEVLOOP_USER', 'devloop'), ('AGENT_USER', 'devloop-agent'),
                                          ('SUPERVISOR_USER', 'supervisor'), ('X_USER', 'ab')])
        text = 'devloop, devloop-agent, supervisor, ab'
        self.assertEqual(defaults.redact(text), text)
        # A user that is one of the trusted GitHub logins stays: the records name them.
        kept = redactor(keep=['U-DL'])
        self.assertEqual(kept.redact('u-dl u-ag'), 'u-dl <AGENT_USER>')

    def test_a_key_file_becomes_the_role_of_its_app(self):
        r = redactor(roles={'acme-merge-bot': 'merger'})
        self.assertEqual(
            r.redact(f'/srv/u-dl/.keys/{PREFIX}implementer.2026-11-23.private-key.pem: 600'),
            "$DEVLOOP_KEY_DIR/<chiave dell'app implementer>: 600")
        self.assertEqual(r.redact('acme-merge-bot.2026-01-01.private-key.pem'),
                         "<chiave dell'app merger>")
        self.assertEqual(r.redact('other.2026-01-01.private-key.pem'), "<chiave di un'app>")
        self.assertNotIn('2026', r.redact(f'{PREFIX}dispatcher.2026-11-23.private-key.pem'))

    def test_what_is_not_a_value_of_the_host_stays(self):
        r = redactor()
        for text in (None, '', 'no paths here', 'docs/devloop/guide.md', '$STATE_DIR/x'):
            self.assertEqual(r.redact(text), text)
        # Twice is once: a redacted text holds no value of the host.
        once = r.redact('/srv/u-dl/clone/.state u-ag')
        self.assertEqual(r.redact(once), once)
        # A path too short to name (/, /srv alone) is never replaced.
        short = redact.Redactor(paths=[('ROOT', '/'), ('X', '/srv'), ('Y', 'relative/path')])
        self.assertEqual(short.paths, [])
        self.assertEqual(short.redact('/srv/x relative/path'), '/srv/x relative/path')

    def test_a_record_stays_valid_json(self):
        r = redactor()
        record = json.dumps({'tail': '/srv/u-dl/clone/.state/x', 'user': 'u-ag'})
        self.assertEqual(json.loads(r.redact(record)),
                         {'tail': '$STATE_DIR/x', 'user': '<AGENT_USER>'})

    def test_the_records_of_the_markers_are_redacted(self):
        # The markers are Base64 that anyone decodes (R1-1): their texts are redacted too, and
        # devloop reads the record back; keys, numbers and identifiers stay as they are.
        r = redactor()
        record = {'type': 'event', 'event': 'host_tests', 'at': '2026-11-24T00:00:00Z',
                  'error': 'see /srv/u-dl/clone/.state/run.log (sudo -u u-ag)',
                  'lines': ['/srv/u-sv/.keys-widget/x', 3, None], 'sha': 'a' * 40}
        body = ledger.render(record)
        self.assertIn('/srv/u-dl/clone/.state', markdown.read_marker(body, 'ledger')['error'])
        published = r.redact(body)
        self.assertEqual(markdown.read_marker(published, 'ledger'), dict(
            record, error='see $STATE_DIR/run.log (sudo -u <AGENT_USER>)',
            lines=['$SUPERVISOR_KEY_DIR/x', 3, None]))
        self.assertNotIn('/srv/', published)
        self.assertTrue(markdown.carries_record(published))
        # Twice is once, and a marker with nothing to redact is the very same line.
        self.assertEqual(r.redact(published), published)
        clean = ledger.render(dict(record, error='none', lines=[]))
        self.assertEqual(r.redact(clean), clean)
        # A story marker after it, and a marker that does not decode, stay where they are.
        broken = '<!-- devloop:ledger:v1 AAAA -->'
        self.assertEqual(r.redact(f'u-ag\n{broken}\n'), f'<AGENT_USER>\n{broken}\n')

    def test_a_conclave_record_stays_bound_to_its_redacted_text(self):
        # The hash of a conclave binds the text of its proposal: it follows the redacted text,
        # and the votes given on it too, so the record read back still holds together.
        r = redactor()
        record = conclave.new_record({'kind': 'blocked', 'comment': 5, 'agent': 'claude',
                                      'step': 'fix'}, 'fix', ['claude', 'codex', 'agy'], 7,
                                     'h' * 64, 'a' * 40, 'a' * 40, conclave.text_hash(''))
        conclave.apply_proposal(record, {
            'reserved': False, 'resolution': 'Spostare /srv/u-dl/clone/.state/x.',
            'reserved_reason': '', 'operator_question': '', 'options': []})
        conclave.apply_vote(record, 0, {'vote': 'APPROVA', 'reason': 'ok', 'reserved': False,
                                        'reserved_reason': '', 'operator_question': '',
                                        'options': []}, record['hash'])
        back = markdown.read_marker(r.redact(conclave.render(record)), 'conclave')
        self.assertEqual(back['proposal']['resolution'], 'Spostare $STATE_DIR/x.')
        bound = conclave.text_hash('Spostare $STATE_DIR/x.')
        self.assertEqual((back['hash'], back['proposal']['hash']), (bound, bound))
        self.assertEqual(back['votes'][0]['proposal_hash'], bound)
        self.assertEqual(back['issue_hash'], 'h' * 64)

    def test_the_identifiers_of_a_record_stay(self):
        # R3-1: a service named as a step, a user named as an agent. In a record they replace
        # only its texts: its identifiers, enumerations and logins stay, and the record is read
        # back and used as before. Its paths and key files are replaced wherever they are.
        record = conclave.new_record({'kind': 'blocked', 'comment': 5, 'agent': 'claude',
                                      'step': 'fix'}, 'fix', ['claude', 'codex', 'agy'], 7,
                                     'h' * 64, 'a' * 40, 'a' * 40, conclave.text_hash(''))
        conclave.apply_proposal(record, {
            'reserved': False, 'resolution': 'Riavviare fix come claude in /srv/u-dl/x.',
            'reserved_reason': '', 'operator_question': '', 'options': []})
        body = conclave.render(record)
        r = redact.Redactor(paths=[('DEVLOOP_HOME', '/srv/u-dl')],
                            users=[('AGENT_USER', 'claude')], names=[('SERVICE_NAME', 'fix')])
        back = markdown.read_marker(r.redact(body), 'conclave')
        self.assertIsNotNone(back)
        self.assertEqual((back['step'], back['block']['step'], back['block']['agent']),
                         ('fix', 'fix', 'claude'))
        self.assertEqual([back['proposal']['agent']] + [v['agent'] for v in back['votes']],
                         ['claude', 'codex', 'agy'])
        resolution = 'Riavviare $SERVICE_NAME come <AGENT_USER> in $DEVLOOP_HOME/x.'
        self.assertEqual(back['proposal']['resolution'], resolution)
        self.assertEqual(back['hash'], conclave.text_hash(resolution))
        self.assertEqual(conclave.consistent(back)[0], conclave.consistent(record)[0])
        # A ledger turn of that agent, and a single path or key file in a record.
        turn = {'type': 'turn', 'state': 'open', 'kind': 'fix', 'agent': 'claude',
                'started_at': '2026-11-24T00:00:00Z', 'ended_at': None, 'outcome': None,
                'telemetry': None, 'cwd': '/srv/u-dl/x',
                'key': f'{PREFIX}implementer.2026-11-23.private-key.pem'}
        read = markdown.read_marker(r.redact(ledger.render(turn)), 'ledger')
        self.assertEqual(read, dict(turn, cwd='$DEVLOOP_HOME/x', key="<chiave di un'app>"))
        # Outside a record the text is redacted as before.
        self.assertEqual(r.redact('fix claude'), '$SERVICE_NAME <AGENT_USER>')

    def test_the_service_name_is_a_parameter(self):
        # R1-3: the name of the service, alone and in the name of its unit, unless it is the
        # documented default.
        r = redact.Redactor.from_map(dict(MAP, paths=dict(MAP['paths'],
                                                          SYSTEMD_DIR='/etc/systemd/system'),
                                          names={'SERVICE_NAME': 'private-loop'}))
        self.assertEqual(
            r.redact('Servizio private-loop: /etc/systemd/system/private-loop.service, '
                     'private-loop@x.service, private-loop è attivo, private-loop-2'),
            'Servizio $SERVICE_NAME: $SYSTEMD_DIR/$SERVICE_NAME.service, '
            'private-loop@x.service, $SERVICE_NAME è attivo, private-loop-2')
        project = redact.Redactor(names=[('SERVICE_NAME', 'devloop@widget')])
        self.assertEqual(project.redact('devloop@widget.service.d devloop@.service'),
                         '$SERVICE_NAME.service.d devloop@.service')
        default = redact.Redactor(names=[('SERVICE_NAME', 'devloop')])
        self.assertEqual(default.redact('devloop.service'), 'devloop.service')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        self.keys = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.keys)
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('DEVLOOP_APP_KEY_DIR', None)
        self.cfg = SimpleNamespace(
            app_key_dir=str(self.keys), agent_user=SimpleNamespace(name=''),
            trusted_users=('brushtyler',),
            apps={'dispatcher': SimpleNamespace(slug='acme-disp', app_id=1)})

    def test_the_runtime_values_prevail_for_state_dir_and_key_dir(self):
        (self.state / redact.MAP_FILE).write_text(json.dumps(MAP))
        lines = []
        r = redact.runtime(self.cfg, self.state, log=lines.append)
        self.assertEqual(lines, [])
        # The dispatcher's own state_dir and key_dir, not the map's.
        self.assertEqual(r.redact(f'{self.state}/worktrees/x {self.keys}/y'),
                         '$STATE_DIR/worktrees/x $DEVLOOP_KEY_DIR/y')
        self.assertEqual(r.redact('/srv/u-dl/clone/.state/x'), '$DEVLOOP_HOME/clone/.state/x')
        # The rest of the map holds.
        self.assertEqual(r.redact('/srv/u-sv/.keys-widget u-ag'),
                         '$SUPERVISOR_KEY_DIR <AGENT_USER>')
        self.assertEqual(r.redact('acme-disp.2026-01-01.private-key.pem'),
                         "<chiave dell'app dispatcher>")
        # $DEVLOOP_APP_KEY_DIR is the key_dir devloop reads its keys from (apps.Identities).
        other = self.state / 'other-keys'
        with mock.patch.dict(os.environ, {'DEVLOOP_APP_KEY_DIR': str(other)}):
            r = redact.runtime(self.cfg, self.state)
        self.assertEqual(r.redact(f'{other}/k'), '$DEVLOOP_KEY_DIR/k')

    def test_without_the_map_the_runtime_values_alone(self):
        lines = []
        me = pwd.getpwuid(os.getuid())
        r = redact.runtime(self.cfg, self.state, log=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn('redactions.json in state_dir missing', lines[0])
        self.assertEqual(r.redact(f'{self.state}/x'), '$STATE_DIR/x')
        if redact._absolute(me.pw_dir) and not str(self.state).startswith(me.pw_dir + '/'):
            self.assertEqual(r.redact(f'{me.pw_dir}/.x'), '$DEVLOOP_HOME/.x')

    def test_a_map_not_valid_is_ignored(self):
        for data in ('{', '[]', '"x"'):
            with self.subTest(data=data):
                (self.state / redact.MAP_FILE).write_text(data)
                lines = []
                r = redact.runtime(self.cfg, self.state, log=lines.append)
                self.assertIn('not valid', lines[0])
                self.assertEqual(r.redact(f'{self.state}/x'), '$STATE_DIR/x')
        # A link is not followed.
        (self.state / redact.MAP_FILE).unlink()
        target = self.keys / 'map.json'
        target.write_text(json.dumps(MAP))
        (self.state / redact.MAP_FILE).symlink_to(target)
        self.assertIsNone(redact.read_map(self.state / redact.MAP_FILE)[0])

    def test_the_agent_user_fills_what_the_map_lacks(self):
        self.cfg.agent_user = SimpleNamespace(name='u-agent-x')
        with mock.patch.object(redact, '_home', return_value='/srv/agent-x-home'):
            r = redact.runtime(self.cfg, self.state)
        self.assertEqual(r.redact('u-agent-x /srv/agent-x-home/.claude'),
                         '<AGENT_USER> $AGENT_HOME/.claude')


class FakeClient:
    def __init__(self):
        self.calls = []
        self.time_left = None

    def comment(self, number, body):
        self.calls.append(('comment', number, body))
        return 1

    def edit_comment(self, comment_id, body):
        self.calls.append(('edit', comment_id, body))

    def submit_review(self, number, *, commit, event, body):
        self.calls.append(('review', number, body))

    def create_pr(self, *, base, head, title, body):
        self.calls.append(('pr', title, body))
        return 7, 'url'

    def issue(self, number):
        return {'body': '/srv/u-dl/clone/.state'}


class FakeOutbox:
    """What the outbox records: the body it is given."""

    def __init__(self):
        self.recorded = []

    def client(self, tracker, actor, permissions):
        outbox = self

        class Client:
            def __getattr__(self, name):
                return getattr(tracker, name)

            def comment(self, number, body):
                outbox.recorded.append(body)
                return tracker.comment(number, body)

            def alert(self, number, body, *, covers):
                outbox.recorded.append(body)
                return tracker.comment(number, body)
        return Client()


class ClientTests(unittest.TestCase):
    def identities(self):
        ids = apps.Identities('acme/widget', {})
        ids._ambient = FakeClient()
        return ids

    def test_every_write_is_redacted(self):
        ids = self.identities()
        ids.redactor = redactor()
        gh = ids.github(apps.DISPATCHER)
        self.assertEqual(gh.comment(5, 'log: /srv/u-dl/clone/.state/run.log'), 1)
        gh.edit_comment(9, body='u-ag')
        gh.submit_review(5, commit='c', event='COMMENT', body='/srv/u-ag/x')
        self.assertEqual(gh.create_pr(base='main', head='b', title='fix /srv/u-dl/x',
                                      body='/srv/u-sv'), (7, 'url'))
        self.assertEqual(ids._ambient.calls, [
            ('comment', 5, 'log: $STATE_DIR/run.log'), ('edit', 9, '<AGENT_USER>'),
            ('review', 5, '$AGENT_HOME/x'), ('pr', 'fix $DEVLOOP_HOME/x', '$SUPERVISOR_HOME')])
        # The reads are not touched, and attributes reach the client.
        self.assertEqual(gh.issue(1), {'body': '/srv/u-dl/clone/.state'})
        gh.time_left = 'limit'
        self.assertEqual(ids._ambient.time_left, 'limit')

    def test_the_outbox_records_the_body_published(self):
        # The outbox reconciles a write on its exact body: it gets the redacted one.
        ids = self.identities()
        ids.redactor = redactor()
        ids.outbox = outbox = FakeOutbox()
        gh = ids.github(apps.DISPATCHER)
        gh.comment(5, '/srv/u-dl/clone/.state/x')
        gh.alert(6, 'u-ag', covers=['pub-1'])
        self.assertEqual(outbox.recorded, ['$STATE_DIR/x', '<AGENT_USER>'])
        self.assertEqual([c[2] for c in ids._ambient.calls], outbox.recorded)

    def test_without_a_redactor_the_client_is_as_before(self):
        ids = self.identities()
        self.assertIs(ids.github(apps.DISPATCHER), ids._ambient)

    def test_devloop_sets_it_at_the_start(self):
        source = (ROOT / 'devloop/__main__.py').read_text(encoding='utf-8')
        self.assertEqual(source.count('identities.redactor = redact.runtime(cfg, state'), 2)


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'),
                                                  ROOT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicationCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def body(self, text):
        path = self.tmp / 'body.md'
        path.write_text(text, encoding='utf-8')
        return str(path)

    def test_host_details(self):
        self.assertEqual(redact.host_details(
            'see /home/u-dl/.ghapps and `/etc/sudoers.d/x`, /var/lib/box, /usr/local/lib/d, '
            '/run/devloop/repos, /root/x, k.2026-01-01.private-key.pem'),
            ['/home/u-dl', '/etc/sudoers.d', '/var/lib/box', '/usr/local/lib/d', '/run/devloop',
             '/root/x', 'k.2026-01-01.private-key.pem'])
        # A list of paths, as PATH.
        self.assertEqual(redact.host_details('PATH=/usr/bin:/home/u-dl/.local/bin'),
                         ['/home/u-dl'])
        # The names of the parameters, the paths of the repository, URLs and the prefixes
        # alone are not details of the host.
        for text in ('$STATE_DIR/worktrees/x', '$DEVLOOP_KEY_DIR', 'ops/host/check-host.sh',
                     'https://example.com/home/x', 'actions/runs/1', 'the prefix `/home/`',
                     'docs/etc/x'):
            with self.subTest(text=text):
                self.assertEqual(redact.host_details(text), [])

    def test_the_texts_of_a_command(self):
        def bodies(*command, stdin=lambda: 'from stdin /home/x'):
            return redact._bodies(list(command), stdin)
        f = self.body('file /home/x')
        self.assertEqual(bodies('gh', 'issue', 'comment', '1', '--body-file', f), ['file /home/x'])
        self.assertEqual(bodies('gh', 'pr', 'comment', '1', f'--body-file={f}'), ['file /home/x'])
        self.assertEqual(bodies('gh', 'issue', 'comment', '1', '-F', f), ['file /home/x'])
        self.assertEqual(bodies('gh', 'issue', 'comment', '1', '-b', 'b'), ['b'])
        self.assertEqual(bodies('gh', 'issue', 'create', '--body', 'b', '--title', 't'),
                         ['b', 't'])
        self.assertEqual(bodies('gh', 'issue', 'comment', '1', '--body-file', '-'),
                         ['from stdin /home/x'])
        self.assertEqual(bodies('gh', 'api', '-X', 'POST', 'x', '--input', f), ['file /home/x'])
        self.assertEqual(bodies('gh', 'api', 'x', '-F', 'n=1', '-f', 'event=COMMENT'),
                         ['n=1', 'event=COMMENT'])
        # R1-2: the fields of gh api read with @ a file, or standard input with @-; -f never.
        self.assertEqual(bodies('gh', 'api', 'x', '-F', f'body=@{f}'), ['file /home/x'])
        self.assertEqual(bodies('gh', 'api', 'x', f'--field=body=@{f}', f'-Fb=@{f}'),
                         ['file /home/x', 'file /home/x'])
        self.assertEqual(bodies('gh', 'api', 'x', '--field', 'body=@-'), ['from stdin /home/x'])
        self.assertEqual(bodies('gh', 'api', 'x', '-f', f'body=@{f}'), [f'body=@{f}'])
        self.assertEqual(bodies('gh', 'api', 'x', '-F', 'body=@missing'), [])
        # Titles, and the short options with their value attached.
        self.assertEqual(bodies('gh', 'issue', 'create', '-t', 't /home/x', '--title=u'),
                         ['t /home/x', 'u'])
        self.assertEqual(bodies('gh', 'issue', 'comment', '1', '-b/home/x', '-tT', f'-F{f}'),
                         ['/home/x', 'T', 'file /home/x'])
        # -t of gh api is its template, -f of gh pr create its --fill: no text.
        self.assertEqual(bodies('gh', 'api', 'x', '-t', '/home/x'), [])
        self.assertEqual(bodies('gh', 'pr', 'create', '-f', '/home/x'), [])
        self.assertEqual(bodies('gh', 'run', 'view', '1', '--log-failed'), [])

    def test_every_form_is_refused(self):
        # R1-2: each form that publishes a detail of the host needs the option, standard input
        # included, which is read once and passed on.
        f = self.body('file /home/u-x/state\n')
        for command in (['gh', 'api', 'repos/o/r/issues/1/comments', '-X', 'POST', '-F',
                         f'body=@{f}'],
                        ['gh', 'api', 'repos/o/r/issues/1/comments', '-F', 'body=@-'],
                        ['gh', 'issue', 'create', '--title', 'on /home/u-x', '--body', 'ok'],
                        ['gh', 'issue', 'comment', '1', '-b/home/u-x/state']):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit):
                    redact.publication_check('role-token', command, False,
                                             stdin=lambda: 'piped /home/u-x/state')
        piped = ['gh', 'api', 'x', '-F', 'body=@-']
        self.assertEqual(redact.publication_check('role-token', piped, False,
                                                  stdin=lambda: 'clean'), 'clean')

    def test_refused_unless_allowed_and_logged(self):
        command = ['gh', 'issue', 'comment', '1', '--body-file',
                   self.body('ok\n/home/u-dl/devloop/.devloop/run.log\n')]
        err, logged = io.StringIO(), []
        with self.assertRaisesRegex(SystemExit, r'role-token: .*details of the host \(1: .*'
                                    r'--allow-host-details'):
            redact.publication_check('role-token', command, False, err=err, log=logged.append)
        self.assertEqual((err.getvalue(), logged), ('', []))
        self.assertIsNone(redact.publication_check('role-token', command, True, err=err,
                                                   log=logged.append))
        self.assertEqual(len(logged), 1)
        self.assertIn('role-token: --allow-host-details: published with 1 details of the host',
                      logged[0])
        # The log names the command, never the details.
        self.assertNotIn('/home/', logged[0])
        self.assertEqual(err.getvalue(), logged[0] + '\n')
        # A clean text passes, with nothing written.
        clean = ['gh', 'issue', 'comment', '1', '--body', '$STATE_DIR/run.log']
        self.assertIsNone(redact.publication_check('role-token', clean, False, err=err,
                                                   log=logged.append))
        self.assertEqual(len(logged), 1)
        # A body read from standard input is checked, and returned to pass on.
        piped = ['gh', 'issue', 'comment', '1', '--body-file', '-']
        self.assertEqual(redact.publication_check('role-token', piped, False,
                                                  stdin=lambda: 'clean'), 'clean')

    def test_allow_option(self):
        self.assertEqual(redact.allow_option(['--allow-host-details', '--state-dir', 'd', '--',
                                              'gh', '--allow-host-details']),
                         (True, ['--state-dir', 'd', '--', 'gh', '--allow-host-details']))
        self.assertEqual(redact.allow_option(['--', 'gh', '--allow-host-details']),
                         (False, ['--', 'gh', '--allow-host-details']))


class TokenScriptTests(unittest.TestCase):
    """Both role scripts refuse, before reading any key, a text with a detail of the host."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        overlay = ssdlc_project.OVERLAY.read_text(encoding='utf-8')
        (self.state / 'config.local.toml').write_text(
            overlay + '\n[apps.coordinator]\napp_id = 7\nslug = "acme-coordinator"\n',
            encoding='utf-8')
        self.body = self.state / 'body.md'
        self.body.write_text('see /home/u-dl/.ghapps/acme-x.2026-01-01.private-key.pem\n')
        environ = mock.patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop('GH_REPO', None)

    def test_refused_before_any_key_is_read(self):
        for name in ('supervisor-token', 'coordinator-token'):
            module = script(name)
            with self.subTest(script=name), \
                    mock.patch.object(module, 'find_key') as find, \
                    mock.patch.object(module.AppAuth, 'token') as mint, \
                    mock.patch.object(module.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, f'{name}: the text to publish still '
                                            'holds details of the host'):
                    module.main(['--state-dir', str(self.state), '--', 'gh', 'issue', 'comment',
                                 '1', '--body-file', str(self.body)])
                find.assert_not_called()
                mint.assert_not_called()
                run.assert_not_called()

    def test_the_fields_of_gh_api_are_read(self):
        # R1-2: a body that gh api loads from a file or from standard input is checked, and
        # standard input, once read, reaches the command.
        for name in ('supervisor-token', 'coordinator-token'):
            module = script(name)
            with self.subTest(script=name), \
                    mock.patch.object(module, 'find_key') as find, \
                    mock.patch.object(module.subprocess, 'run') as run:
                with self.assertRaisesRegex(SystemExit, 'details of the host'):
                    module.main(['--state-dir', str(self.state), '--', 'gh', 'api',
                                 'repos/o/r/issues/1/comments', '-F', f'body=@{self.body}'])
                with mock.patch('sys.stdin', io.StringIO('piped /home/u-dl/x')), \
                        self.assertRaisesRegex(SystemExit, 'details of the host'):
                    module.main(['--state-dir', str(self.state), '--', 'gh', 'api', 'x', '-F',
                                 'body=@-'])
                find.assert_not_called()
                run.assert_not_called()
            configured = SimpleNamespace(app_id=7, slug='acme-x')
            cfg = SimpleNamespace(repo='brushtyler/ssdlc', app_key_dir=str(self.state),
                                  apps={apps.SUPERVISOR: configured, apps.COORDINATOR: configured})
            seen = {}

            def fake_run(command, env, **kw):
                seen.update(kw, command=command)
                return SimpleNamespace(returncode=0)
            with self.subTest(script=name, stdin='clean'), \
                    mock.patch.object(module, 'load_config', return_value=cfg), \
                    mock.patch.object(module, 'find_key', return_value=self.body), \
                    mock.patch.object(module.AppAuth, 'token', return_value='t'), \
                    mock.patch.object(module.subprocess, 'run', fake_run), \
                    mock.patch('sys.stdin', io.StringIO('$STATE_DIR/run.log')):
                command = ['gh', 'api', 'x', '-F', 'body=@-']
                self.assertEqual(module.main(['--', *command]), 0)
                self.assertEqual((seen['command'], seen['input']), (command, '$STATE_DIR/run.log'))

    def test_published_anyway_with_the_option_logged(self):
        for name in ('supervisor-token', 'coordinator-token'):
            module = script(name)
            configured = SimpleNamespace(app_id=7, slug='acme-x')
            cfg = SimpleNamespace(repo='brushtyler/ssdlc', app_key_dir=str(self.state),
                                  apps={apps.SUPERVISOR: configured, apps.COORDINATOR: configured})
            logged, seen = [], {}

            def fake_run(command, env):
                seen['command'] = command
                return SimpleNamespace(returncode=0)
            with self.subTest(script=name), \
                    mock.patch.object(module, 'load_config', return_value=cfg), \
                    mock.patch.object(module, 'find_key', return_value=self.body), \
                    mock.patch.object(module.AppAuth, 'token', return_value='t'), \
                    mock.patch.object(module.subprocess, 'run', fake_run), \
                    mock.patch.object(redact, '_syslog', logged.append), \
                    mock.patch('sys.stderr', io.StringIO()) as err:
                command = ['gh', 'issue', 'comment', '1', '--body-file', str(self.body)]
                self.assertEqual(module.main(['--allow-host-details', '--', *command]), 0)
                self.assertEqual(seen['command'], command)
                self.assertEqual(len(logged), 1)
                self.assertIn(f'{name}: --allow-host-details', logged[0])
                self.assertIn(logged[0], err.getvalue())


if __name__ == '__main__':
    unittest.main()
