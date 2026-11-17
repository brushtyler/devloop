"""Headless invocation of the agent CLIs with schema-validated final output."""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .markdown import header, parse_version
from .schema import load_schema, schema_path, validate, SchemaError
from .unavailability import reset_seconds
from .workspace import git_config_env, partial_text, run_in_group

MODES = ('write', 'review')
# The tools that start sub-agents, denied in every claude turn (issue #219): a headless turn
# ends with the final answer and does not wait for a sub-agent still running in the background,
# and these tools can run in the background. Agent was called Task before; Workflow runs its
# sub-agents in the background only.
CLAUDE_DENIED_TOOLS = ('Agent', 'Task', 'Workflow')
# A blocked turn whose reason says that background work was still running (issue #219): the
# turn ended before its own work, a failure of the tool rather than of the change. Both
# patterns must match the same sentence. "Before" counts only with a verb of completion
# ("prima che l'audit si concludesse"): "prima che venga introdotto il job" describes a
# decision to take, not work left running. A reason that asks for the operator's decision
# stays a block, whatever else it says.
_BACKGROUND = re.compile(r'background|parallel|sotto-?agent|sub-?agent', re.I)
_UNFINISHED = re.compile(r'in corso|ancora attiv|still (running|active|in progress)'
                         r'|not (yet )?(finish|complet|done|conclu)'
                         r'|non (ancora )?(conclus|terminat|finit|complet)'
                         r'|prima che\b.*\b(conclud|conclus|termin|finiss|finit|complet)'
                         r'|before\b.*\b(finish|complet|conclu|ended\b|(was|were) done)', re.I)
_DECISION = re.compile(r'decisione|decidere|decida|operatore|\bdecision|\bdecide|operator'
                       r'|product call', re.I)


def background_unfinished(reason):
    """Whether the ``blocked_reason`` of a turn says that background work (sub-agents, parallel
    tasks) was still running when the turn ended (issue #219). A reason that asks for a
    decision of the operator is never one: the block is the operator's."""
    if _DECISION.search(reason or ''):
        return False
    return any(_BACKGROUND.search(s) and _UNFINISHED.search(s)
               for s in re.split(r'[.;:!?\n]+', reason or ''))
CREDENTIAL_VARS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN',
                   'SSH_AUTH_SOCK', 'GIT_CONFIG_PARAMETERS')


def isolated_env(gh_config_dir, base=None):
    """Environment for agent CLIs and the test command: no GitHub credentials in reach of `gh`
    or `git`.

    - ``gh`` gets an empty config directory and no token variables;
    - git may only use local repositories (``GIT_ALLOW_PROTOCOL=file``), so neither SSH keys nor
      HTTPS credentials can be used, whatever the config files say;
    - as further layers, credential helpers, askpass programs (``GIT_ASKPASS`` overrides
      ``core.askPass``), preconfigured authentication headers and SSH are disabled;
    - ``DEVLOOP_APP_KEY_DIR`` points to a directory without keys, so the GitHub App helpers in
      the worktree (``devloop.apps``, ``scripts/agent-token.py``) find no app key: the dispatcher
      does every GitHub write.

    This prevents accidental use of the operator's credentials, not deliberate access: code
    running as the same system user can still read files. Real isolation needs a dedicated
    system user or a container.
    """
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in CREDENTIAL_VARS and not k.startswith('GIT_CONFIG_')}
    env.update(git_config_env([('credential.helper', ''), ('http.extraHeader', '')]))
    env.update({'GH_CONFIG_DIR': str(gh_config_dir), 'GIT_ALLOW_PROTOCOL': 'file',
                'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': 'false', 'SSH_ASKPASS': 'false',
                'GIT_SSH_COMMAND': 'false',
                'DEVLOOP_APP_KEY_DIR': str(Path(gh_config_dir) / 'no-app-keys')})
    return env


class AgentError(RuntimeError):
    """A failed turn. ``log``: the turn's local log; ``tail``: for a turn the dispatcher stopped
    (a timeout, issue #215), the last lines of its output, cleaned by ``output_tail``, or ''."""

    tail = ''

    def __init__(self, message, log=None):
        super().__init__(message)
        self.log = log


TAIL_LINES = 10
TAIL_WIDTH = 300
COMMAND_WIDTH = 200
_SECRET_NAME = re.compile(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', re.I)
# Values of secret-looking variables shorter than this are replaced only as whole words, and
# those shorter than ``_SECRET_MIN`` only after their name (``_ASSIGNED``): a value such as
# ``1`` or ``yes`` would otherwise blank every occurrence of it in the output.
_SECRET_WORD = 8
_SECRET_MIN = 4
_PEM = re.compile(r'-----BEGIN [^-]+-----.*?(?:-----END [^-]+-----|\Z)', re.S)
_CREDENTIAL = re.compile(r'(?<![A-Za-z0-9_])(?:sk-[A-Za-z0-9_-]{20,}'
                         r'|(?:ghp_|ghs_|gho_|ghu_|ghr_|github_pat_)[A-Za-z0-9_]{20,}'
                         r'|AKIA[A-Z0-9]{16}|Bearer\s+[A-Za-z0-9._~+/-]{20,}=*)(?![A-Za-z0-9_])')
# The value assigned to a secret-looking name, whatever its length: ``DB_PASSWORD=x``,
# ``api-key: x``, ``"token": "x"``, also inside the JSON events of a CLI (``\"x\"``). The word
# must be a whole part of the name: ``KeyError: 'x'`` and ``input_tokens: 3`` stay.
_ASSIGNED = re.compile(
    r'(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_.-])*(?:key|token|secret|password|passwd|credentials?)'
    r'(?:[_.-][A-Za-z0-9]+)*(?![A-Za-z0-9])\\?["\']?\s*[=:]\s*\\?["\']?)'
    r'([^\s"\'`\\,;&|]+)', re.I)
# A terminal escape sequence (colours, cursor, titles), raw or as the JSON events of a CLI
# write it (``\u001b``): it would hide what follows it, an absolute path for example.
_ESC = r'(?:\x1b|\\u001[bB]|\\x1[bB]|\\033)'
_ANSI = re.compile(_ESC + r'(?:\[[0-?]*[ -/]*[@-~]'  # CSI: colours, cursor
                   r'|\][^\x07\x1b\n]*?(?:\x07|\\u0007|' + _ESC + r'\\{1,2})'  # OSC: titles, links
                   r'|[@-Z^_])')  # other two-character sequences
# An absolute path: a slash not preceded by a word, a dot, a tilde or another slash (save the
# ``\n``, ``\t`` and ``\r`` of a JSON string), followed by one or more segments. Relative
# paths, ``~/…`` and URLs (``//`` after the scheme) do not match.
_ABSOLUTE = re.compile(r'(?:(?<=\\[nrt])|(?<![\w.~/\\]))(?:/[^\s/\'"`,;:()\[\]{}<>|\\]+)+/?')


def _local_path(match):
    name = match.group(0).rstrip('/').rsplit('/', 1)[-1]
    return f'…/{name}' if name else '…'


def public_text(text, workdir=None, env=None):
    """``text`` written by an agent, cleaned for a public comment (issue #215): terminal escape
    sequences removed; private keys, credentials, values assigned to secret-looking names and
    the values of secret-looking variables of ``env`` (default ``os.environ``) replaced with
    ``[REDACTED]``; paths inside ``workdir`` made relative and any other absolute path reduced
    to its last segment."""
    text = _ANSI.sub('', text)
    values = {v for k, v in (os.environ if env is None else env).items()
              if _SECRET_NAME.search(k) and isinstance(v, str) and len(v.strip()) >= _SECRET_MIN}
    text = _PEM.sub('[REDACTED]', text)
    for secret in sorted(values, key=lambda v: (-len(v), v)):
        if len(secret) >= _SECRET_WORD:
            text = text.replace(secret, '[REDACTED]')
        else:
            text = re.sub(r'(?<![A-Za-z0-9_])' + re.escape(secret) + r'(?![A-Za-z0-9_])',
                          '[REDACTED]', text)
    text = _CREDENTIAL.sub('[REDACTED]', text)
    text = _ASSIGNED.sub(r'\1[REDACTED]', text)
    if workdir is not None:
        roots = {str(Path(workdir)), os.path.realpath(workdir)}
        for root in sorted((r.rstrip('/') for r in roots if r.rstrip('/')), key=len,
                           reverse=True):
            text = re.sub(re.escape(root) + r'(?=[/\s\'"`:,;)\]}\\]|$)', '.', text,
                          flags=re.M)
    return _ABSOLUTE.sub(_local_path, text)


def _cut(line, width):
    return line if len(line) <= width else line[:width - 1] + '…'


def output_tail(stdout, stderr, workdir=None, env=None, lines=TAIL_LINES, width=TAIL_WIDTH):
    """The last ``lines`` non-blank lines of a turn's output (stdout, then stderr), cleaned by
    ``public_text`` for a public comment (issue #215), each cut to ``width`` characters. ''
    when the turn wrote nothing."""
    text = public_text(f'{stdout or ""}\n{stderr or ""}', workdir, env)
    kept = [line.rstrip() for line in text.splitlines() if line.strip()][-lines:]
    return '\n'.join(_cut(line, width) for line in kept)


def last_command(kind, stdout):
    """The last command the turn's output says the agent ran or started, as written, or None
    when the CLI does not expose it (issue #215). Only codex does, with ``--json``: its
    ``command_execution`` items. claude and agy write their JSON only at the end, without the
    commands."""
    if kind != 'codex':
        return None
    found = None
    for line in (stdout or '').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get('item') if isinstance(event, dict) else None
        if (isinstance(item, dict) and item.get('type') == 'command_execution'
                and isinstance(item.get('command'), str) and item['command'].strip()):
            found = item['command']
    return found


def public_command(kind, stdout, workdir=None, env=None, width=COMMAND_WIDTH):
    """``last_command`` cleaned by ``public_text`` for the turn's ledger marker, on one line
    and cut to ``width`` characters; None when the CLI does not expose it."""
    command = last_command(kind, stdout)
    if command is None:
        return None
    return _cut(' '.join(public_text(command, workdir, env).split()), width) or None


class EnvelopeError(AgentError):
    """The CLI's JSON envelope says, explicitly, that the turn failed."""


class ModelUnavailable(AgentError):
    """A failed turn whose output says that the model is unavailable (issue #121). The message
    is the one the plain failure would have; ``reason`` says what matched. ``resets_at``: the
    epoch the error announces the model back at (``Resets in <duration>`` after the failure
    was detected, #151), or None when it announces none."""

    def __init__(self, message, log=None, reason='', resets_at=None):
        super().__init__(message, log)
        self.reason = reason
        self.resets_at = resets_at


class PermissionDenied(AgentError):
    """An agy turn that exited 0 with no output because headless mode denied a permission it
    cannot ask for (issue #180): a failure of the tool, not of the change. ``permission`` is the
    denied one, as agy names it; ``target`` the last action agy's JSON lists as denied, as
    compact JSON, or None when agy does not report it."""

    def __init__(self, message, log=None, permission='', target=None):
        super().__init__(message, log)
        self.permission = permission
        self.target = target


class ModelsUnavailable(AgentError):
    """Every model of an agent's chain was unavailable, or left out, in one action; or, with
    ``last_failed``, the last model tried failed otherwise after the others had been. ``tried``:
    ``(model, why)`` pairs, in order."""

    def __init__(self, key, tried, log=None, last_failed=False):
        listed = '\n'.join(f'- `{model}`: {why}' for model, why in tried)
        headline = (f'the last model tried for `{key}` failed' if last_failed
                    else f'no model of `{key}` was available')
        super().__init__(f'{headline}; models tried:\n\n{listed}', log)
        self.tried = list(tried)


def unavailable_reason(agent, returncode, text):
    """Why a failed turn of ``agent`` counts as its model being unavailable, or None. Call it
    only for a non-zero exit (``returncode``) or an explicit error envelope (``returncode``
    None); a valid result, a parse or schema error and a timeout never count."""
    if returncode is not None and returncode in agent.unavailable_exit_codes:
        return f'exit code {returncode}'
    for pattern in agent.unavailable_patterns:
        match = re.search(pattern, text or '', re.IGNORECASE)
        if match:
            start = text.rfind('\n', 0, match.start()) + 1
            end = text.find('\n', match.end())
            line = text[start:end if end >= 0 else len(text)].strip()
            return f'`{pattern}` in "{line[:200]}"'
    return None


TELEMETRY_FIELDS = ('cost_usd', 'duration_ms', 'num_turns', 'input_tokens', 'output_tokens',
                    'cache_read_input_tokens', 'cache_creation_input_tokens')


def _count(value):
    """A count the CLI reported, or None: never 0 for a value that is missing or not a count."""
    return value if type(value) is int and value >= 0 else None


def _amount(value):
    return value if type(value) in (int, float) and value >= 0 else None


def telemetry(kind, stdout):
    """``(fields, models)`` that one turn's output attests (#133). ``fields`` has every key of
    ``TELEMETRY_FIELDS``, None where the CLI did not report the value; ``models`` is the list of
    model identifiers the CLI reports, or None. Nothing here is authenticated: it is what the
    CLI wrote.

    - claude (``--output-format json``): ``total_cost_usd``, ``duration_ms``, ``num_turns``,
      ``usage`` and the keys of ``modelUsage``;
    - codex (``--json``): the token usage of every ``turn.completed`` event, summed; its
      ``input_tokens`` already includes the cached input (``cached_input_tokens``). No cost,
      duration or model;
    - agy: nothing known."""
    fields = dict.fromkeys(TELEMETRY_FIELDS)
    models = None
    if kind == 'claude':
        try:
            envelope = json.loads(stdout or '')
        except json.JSONDecodeError:
            return fields, None
        if not isinstance(envelope, dict):
            return fields, None
        fields['cost_usd'] = _amount(envelope.get('total_cost_usd'))
        fields['duration_ms'] = _count(envelope.get('duration_ms'))
        fields['num_turns'] = _count(envelope.get('num_turns'))
        usage = envelope.get('usage') if isinstance(envelope.get('usage'), dict) else {}
        for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                    'cache_creation_input_tokens'):
            fields[key] = _count(usage.get(key))
        used = envelope.get('modelUsage')
        if isinstance(used, dict):
            models = sorted(k for k in used if isinstance(k, str) and k) or None
    elif kind == 'codex':
        names = {'input_tokens': 'input_tokens', 'output_tokens': 'output_tokens',
                 'cached_input_tokens': 'cache_read_input_tokens'}
        for line in (stdout or '').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not (isinstance(event, dict) and event.get('type') == 'turn.completed'
                    and isinstance(event.get('usage'), dict)):
                continue
            for source, target in names.items():
                value = _count(event['usage'].get(source))
                if value is not None:
                    fields[target] = (fields[target] or 0) + value
    return fields, models


def build_command(agent, mode, prompt, *, schema_file, out_file):
    """Return ``(argv, stdin)`` for one headless agent turn in the current directory."""
    if mode not in MODES:
        raise ValueError(mode)
    extra = list(agent.write_args if mode == 'write' else agent.review_args)
    if agent.kind == 'claude':
        schema = Path(schema_file).read_text(encoding='utf-8')
        argv = ['claude', '-p', '--model', agent.model, '--effort', agent.effort,
                '--output-format', 'json', '--json-schema', schema,
                '--disallowedTools', *CLAUDE_DENIED_TOOLS, *extra]
        return argv, prompt
    if agent.kind == 'codex':
        # A reviewer runs in a disposable copy of the PR worktree (Workspace.review_copy): it
        # may write there and in the temporary directory, so that tests creating temporary
        # files can run, but not elsewhere (no extra writable roots from the operator's
        # config) and without network.
        review = ['-c', 'sandbox_workspace_write.network_access=false',
                  '-c', 'sandbox_workspace_write.writable_roots=[]'] if mode == 'review' else []
        # Not --ephemeral: the session transcript in ~/.codex/sessions is what `devloop follow`
        # reads while the turn runs. --json: the events on stdout carry the token usage (#133);
        # the final message still comes from -o.
        argv = ['codex', 'exec', '--json', '-m', agent.model,
                '-c', f'model_reasoning_effort="{agent.effort}"', '-s', 'workspace-write',
                *review, '--output-schema', str(schema_file), '-o', str(out_file), *extra, '-']
        return argv, prompt
    if agent.kind == 'agy':
        argv = ['agy', '-p', prompt, '--model', agent.model, '--effort', agent.effort,
                '--output-format', 'json', '--json-schema', str(schema_file),
                '--mode', 'accept-edits' if mode == 'write' else 'plan',
                '--settings', json.dumps(agy_settings(agent, mode)), *extra]
        return argv, None
    raise ValueError(agent.kind)


def agy_settings(agent, mode):
    """The settings of one agy turn (issue #122), applied by ``--settings`` to that session only:
    the operator's profile stays as it is. Only the configured command prefixes of the mode are
    allowed; reads and edits stay inside the turn's working directory, which agy enforces
    without rules."""
    return {'permissions': {'allow': [f'command({c})' for c in agy_commands(agent, mode)]}}


def agy_commands(agent, mode):
    """The command prefixes an agy turn of ``mode`` may run: those of its ``--settings``."""
    return agent.write_commands if mode == 'write' else agent.review_commands


def agy_prompt_block(agent, mode):
    """What an agy turn is told about its shell commands (issue #155), generated from the same
    list as its ``--settings``: headless agy cannot ask for a permission, and a command outside
    the list ends the turn with no result. Empty for the other agents."""
    if agent.kind != 'agy':
        return ''
    commands = agy_commands(agent, mode)
    if commands:
        allowed = ('The only shell commands you may run in this turn are those that start with '
                   'one of these prefixes:\n\n' + ''.join(f'- `{c}`\n' for c in commands))
    else:
        allowed = 'You may run no shell command in this turn.\n'
    return ('\n\n## Shell commands\n\nThis session cannot ask for permissions. ' + allowed +
            '\nRead and search files only with your own file tools, using paths relative to the '
            'working directory. Do not use `grep`, `ls`, `cat`, `find`, `sed` or any other shell '
            'command to read or search files. Any command not in the list above is denied, and '
            'a denied command ends the turn with no result.\n'
            '\nRun commands so that their output stays short and complete: for example a single '
            'test module at a time, with `-q`. If an output is truncated, run the command again '
            'on a narrower scope. Never read logs or files outside the working directory, '
            'including the logs of your own background tasks: reading them needs a permission '
            'this session cannot ask for, and ends the turn with no result.\n')


def turn_prompt(agent, mode, prompt):
    """The prompt one turn of ``agent`` runs with: for agy, followed by its command block."""
    return prompt + agy_prompt_block(agent, mode)


NO_OUTPUT = 'jetski: no output produced'


def denied_actions(kind, stdout):
    """The ``denied_actions`` of an agy JSON envelope, as compact JSON, or None (issue #155):
    the actions headless agy denied, for example ``[{"action":"command",...}]``."""
    if kind != 'agy':
        return None
    try:
        envelope = json.loads(stdout or '')
    except json.JSONDecodeError:
        return None
    denied = envelope.get('denied_actions') if isinstance(envelope, dict) else None
    if not denied:
        return None
    return json.dumps(denied, ensure_ascii=False, separators=(',', ':'))[:500]


PERMISSION = re.compile(r'a tool required the "([^"]+)" permission')


def denied_permission(stopped):
    """The permission named by agy's ``jetski: no output produced`` diagnostic (``stopped``, from
    ``no_output_reason``), for example ``read_file``, or None (issue #180)."""
    match = PERMISSION.search(stopped or '')
    return match.group(1) if match else None


def denied_target(kind, stdout):
    """The last action agy's JSON envelope lists in ``denied_actions``, with the arguments agy
    reports for it, as compact JSON, or None (issue #180)."""
    if kind != 'agy':
        return None
    try:
        envelope = json.loads(stdout or '')
    except json.JSONDecodeError:
        return None
    denied = envelope.get('denied_actions') if isinstance(envelope, dict) else None
    if not isinstance(denied, list) or not denied:
        return None
    return json.dumps(denied[-1], ensure_ascii=False, separators=(',', ':'))[:500]


def no_output_reason(kind, stdout, stderr):
    """Why an agy turn stopped without output, or None (issue #122): in headless mode a denied
    permission ends the session with exit 0, no output and ``jetski: no output produced …`` on
    stderr. The message counts only as a diagnostic of the CLI: anywhere on stderr, and on stdout
    only as a line of its own, so that a structured answer quoting it is not taken for it."""
    if kind != 'agy':
        return None
    lines = [line for line in (stderr or '').splitlines() if NO_OUTPUT in line]
    lines += [line for line in (stdout or '').splitlines() if line.strip().startswith(NO_OUTPUT)]
    if lines:
        return lines[0].strip()[:500]
    if not (stdout or '').strip():
        return 'no output'
    return None


def envelope_error(kind, envelope):
    """The message of an envelope (a dict) that says, explicitly, that the turn failed, or None."""
    if kind == 'claude' and envelope.get('is_error'):
        return (f'claude reported an error: {envelope.get("subtype")}: '
                f'{str(envelope.get("result"))[:500]}')
    if kind == 'agy' and envelope.get('status') not in (None, 'SUCCESS'):
        return f'agy status {envelope.get("status")}: {str(envelope.get("response"))[:500]}'
    return None


def extract_output(kind, stdout, out_file):
    """The structured final message of one agent turn, before schema validation."""
    if kind == 'codex':
        try:
            text = Path(out_file).read_text(encoding='utf-8')
        except OSError as exc:
            raise AgentError(f'codex wrote no final message: {exc}') from exc
        return json.loads(text)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AgentError(f'{kind} output is not JSON: {exc}') from exc
    if not isinstance(envelope, dict):
        raise AgentError(f'{kind} output is not a JSON object: {type(envelope).__name__}')
    error = envelope_error(kind, envelope)
    if error:
        raise EnvelopeError(error)
    output = envelope.get('structured_output')
    if output is None:
        raise AgentError(f'{kind} returned no structured_output')
    return output


def left_no_output(kind, stdout, out_file):
    """Whether a failed invocation left no output at all, or only an explicit error envelope:
    only then may its text say that the model is unavailable (#121). A structured result, valid
    against the schema or not, and output that cannot be parsed (not JSON, or not an object
    where the CLI writes an envelope) take precedence over any text match."""
    if kind == 'codex':
        try:
            text = Path(out_file).read_text(encoding='utf-8')
        except OSError:
            return True
        return not text.strip()
    if not (stdout or '').strip():
        return True
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    if not isinstance(envelope, dict):
        return False
    return bool(envelope_error(kind, envelope)) or envelope.get('structured_output') is None


class AgentRunner:
    """``on_turn(agent, prompt_path)``, if given, is called when a turn starts. ``stall``
    (``stall.StallWatch``), if given, checks each turn for stalls while it runs.

    ``last_report`` is the ``telemetry`` of the latest turn, ``(fields, models)``, set whether
    the turn succeeded or failed; None when the CLI did not complete (timeout, stall, failed
    start), so that nothing is attested. It belongs to the calling thread: parallel actions
    (#176) each read the report of their own turn. ``last_command``, in the same way, is the
    last command the latest turn's output exposes (``public_command``), whether the turn ended
    or was stopped, or None. ``clock`` dates the failures that announce when the model is back
    (#151)."""

    def __init__(self, log_dir, timeout, run=subprocess.run, env=None, on_turn=None, stall=None,
                 clock=time.time):
        self.log_dir = Path(log_dir)
        self._clock = clock
        self.timeout = timeout
        self._run = run
        self._env = env
        self._on_turn = on_turn
        self._stall = stall
        self._versions = {}
        self._local = threading.local()

    @property
    def last_report(self):
        return getattr(self._local, 'report', None)

    @last_report.setter
    def last_report(self, report):
        self._local.report = report

    @property
    def last_command(self):
        return getattr(self._local, 'command', None)

    @last_command.setter
    def last_command(self, command):
        self._local.command = command

    def _resets_at(self, text):
        """The epoch the error in ``text`` announces the model back at, or None (#151)."""
        seconds = reset_seconds(text)
        return None if seconds is None else self._clock() + seconds

    def version(self, agent):
        """The CLI version, cached once known; a failed lookup is retried on the next call."""
        if agent.key in self._versions:
            return self._versions[agent.key]
        try:
            done = self._run(list(agent.version_cmd), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        version = parse_version(done.stdout + done.stderr)
        if version:
            self._versions[agent.key] = version
        return version

    def header(self, agent):
        return header(agent.display_name, self.version(agent), agent.model_label, agent.effort)

    def run(self, agent, mode, prompt, schema_name, workdir, label):
        """Run one turn in ``workdir`` and return its schema-valid output."""
        self.last_report = None
        self.last_command = None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stem = self.log_dir / f'{time.strftime("%Y%m%dT%H%M%S")}-{label}-{agent.key}'
        out_file = stem.with_suffix('.last.json')
        prompt = turn_prompt(agent, mode, prompt)
        argv, stdin = build_command(agent, mode, prompt, schema_file=schema_path(schema_name),
                                    out_file=out_file)
        prompt_file = stem.with_suffix('.prompt.md')
        prompt_file.write_text(prompt, encoding='utf-8')
        if self._on_turn:
            self._on_turn(agent, prompt_file)
        log = stem.with_suffix('.log')
        kwargs = {'env': self._env} if self._env is not None else {}
        # Its own process group: a timeout also stops the commands the CLI left in the background.
        run = self._run
        if run is subprocess.run:
            run = run_in_group
            if self._stall is not None:
                kwargs.update(watch=self._stall.turn(agent.key, workdir),
                              watch_interval=self._stall.interval)
        def write_log(status, stdout, stderr):
            log.write_text(f'$ {argv[0]} ({mode})\n{status}\n--- stdout\n{stdout}'
                           f'\n--- stderr\n{stderr}', encoding='utf-8')

        def stopped_log(status, stdout='', stderr=''):
            # The comment of a failed turn cites the log: it exists even when the turn did not
            # end on its own (issue #215). A log that cannot be written does not hide why the
            # turn failed.
            try:
                write_log(status, partial_text(stdout), partial_text(stderr))
            except OSError:
                pass

        try:
            done = run(argv, input=stdin, cwd=workdir, capture_output=True, text=True,
                       timeout=self.timeout, **kwargs)
        except AgentError as exc:  # the stall check stopped the turn
            out, err = (partial_text(p) for p in (getattr(exc, 'partial_output', None)
                                                  or ('', '')))
            stopped_log(f'stopped: {exc}', out, err)
            self.last_command = public_command(agent.kind, out, workdir, self._env)
            exc.log = log
            raise
        except subprocess.TimeoutExpired as exc:
            out, err = partial_text(exc.output), partial_text(exc.stderr)
            message = f'{agent.key} timed out after {self.timeout}s'
            stopped_log(message, out, err)
            self.last_command = public_command(agent.kind, out, workdir, self._env)
            error = AgentError(message, log)
            error.tail = output_tail(out, err, workdir, self._env)
            raise error from exc
        except OSError as exc:
            stopped_log(f'could not start: {exc}')
            raise AgentError(f'{agent.key} could not start: {exc}', log) from exc
        self.last_report = telemetry(agent.kind, done.stdout)
        self.last_command = public_command(agent.kind, done.stdout, workdir, self._env)
        write_log(f'exit {done.returncode}', done.stdout, done.stderr)
        text = f'{done.stdout}\n{done.stderr}'
        stopped = no_output_reason(agent.kind, done.stdout, done.stderr)
        denied = denied_actions(agent.kind, done.stdout)
        denied = f'; denied_actions: {denied}' if denied else ''
        if done.returncode == 0 and stopped:
            # Not an empty result: the turn failed, for example on a permission it could not ask.
            message = f'{agent.key} stopped without output: {stopped}{denied}'
            permission = denied_permission(stopped)
            if permission:
                raise PermissionDenied(message, log, permission,
                                       denied_target(agent.kind, done.stdout))
            raise AgentError(message, log)
        if done.returncode != 0:
            message = f'{agent.key} exited with {done.returncode}'
            if stopped and stopped != 'no output':
                message += f': {stopped}'
            message += denied
            if left_no_output(agent.kind, done.stdout, out_file):
                reason = unavailable_reason(agent, done.returncode, text)
                if reason:
                    raise ModelUnavailable(message, log, reason, self._resets_at(text))
            # A result or a parse error: the turn failed, but not for lack of a model (#121).
            raise AgentError(message, log)
        try:
            output = extract_output(agent.kind, done.stdout, out_file)
            return validate(output, load_schema(schema_name))
        except EnvelopeError as exc:
            reason = unavailable_reason(agent, None, text)
            if reason:
                raise ModelUnavailable(str(exc), log, reason, self._resets_at(text)) from exc
            if denied:
                raise EnvelopeError(f'{exc}{denied}', log) from exc
            exc.log = log
            raise
        except AgentError as exc:
            if denied:
                raise AgentError(f'{exc}{denied}', log) from exc
            exc.log = log
            raise
        except (SchemaError, json.JSONDecodeError) as exc:
            raise AgentError(f'{agent.key} output does not match {schema_name}: {exc}', log) from exc
