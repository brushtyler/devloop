"""Headless invocation of the agent CLIs with schema-validated final output."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .markdown import header, parse_version
from .schema import load_schema, schema_path, validate, SchemaError
from .workspace import git_config_env, run_in_group

MODES = ('write', 'review')
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
    def __init__(self, message, log=None):
        super().__init__(message)
        self.log = log


class EnvelopeError(AgentError):
    """The CLI's JSON envelope says, explicitly, that the turn failed."""


class ModelUnavailable(AgentError):
    """A failed turn whose output says that the model is unavailable (issue #121). The message
    is the one the plain failure would have; ``reason`` says what matched."""

    def __init__(self, message, log=None, reason=''):
        super().__init__(message, log)
        self.reason = reason


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
                '--output-format', 'json', '--json-schema', schema, *extra]
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
            'a denied command ends the turn with no result.\n')


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
    start), so that nothing is attested."""

    def __init__(self, log_dir, timeout, run=subprocess.run, env=None, on_turn=None, stall=None):
        self.log_dir = Path(log_dir)
        self.timeout = timeout
        self._run = run
        self._env = env
        self._on_turn = on_turn
        self._stall = stall
        self._versions = {}
        self.last_report = None

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
        try:
            done = run(argv, input=stdin, cwd=workdir, capture_output=True, text=True,
                       timeout=self.timeout, **kwargs)
        except AgentError as exc:  # the stall check stopped the turn
            exc.log = log
            raise
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f'{agent.key} timed out after {self.timeout}s', log) from exc
        except OSError as exc:
            raise AgentError(f'{agent.key} could not start: {exc}', log) from exc
        self.last_report = telemetry(agent.kind, done.stdout)
        log.write_text(f'$ {argv[0]} ({mode})\nexit {done.returncode}\n--- stdout\n{done.stdout}'
                       f'\n--- stderr\n{done.stderr}', encoding='utf-8')
        text = f'{done.stdout}\n{done.stderr}'
        stopped = no_output_reason(agent.kind, done.stdout, done.stderr)
        denied = denied_actions(agent.kind, done.stdout)
        denied = f'; denied_actions: {denied}' if denied else ''
        if done.returncode == 0 and stopped:
            # Not an empty result: the turn failed, for example on a permission it could not ask.
            raise AgentError(f'{agent.key} stopped without output: {stopped}{denied}', log)
        if done.returncode != 0:
            message = f'{agent.key} exited with {done.returncode}'
            if stopped and stopped != 'no output':
                message += f': {stopped}'
            message += denied
            if left_no_output(agent.kind, done.stdout, out_file):
                reason = unavailable_reason(agent, done.returncode, text)
                if reason:
                    raise ModelUnavailable(message, log, reason)
            # A result or a parse error: the turn failed, but not for lack of a model (#121).
            raise AgentError(message, log)
        try:
            output = extract_output(agent.kind, done.stdout, out_file)
            return validate(output, load_schema(schema_name))
        except EnvelopeError as exc:
            reason = unavailable_reason(agent, None, text)
            if reason:
                raise ModelUnavailable(str(exc), log, reason) from exc
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
