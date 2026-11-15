"""Headless invocation of the agent CLIs with schema-validated final output."""

import json
import os
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
        argv = ['codex', 'exec', '--ephemeral', '-m', agent.model,
                '-c', f'model_reasoning_effort="{agent.effort}"', '-s', 'workspace-write',
                *review, '--output-schema', str(schema_file), '-o', str(out_file), *extra, '-']
        return argv, prompt
    if agent.kind == 'agy':
        argv = ['agy', '-p', prompt, '--model', agent.model, '--effort', agent.effort,
                '--output-format', 'json', '--json-schema', str(schema_file),
                '--mode', 'accept-edits' if mode == 'write' else 'plan', *extra]
        return argv, None
    raise ValueError(agent.kind)


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
    if kind == 'claude' and envelope.get('is_error'):
        raise AgentError(f'claude reported an error: {envelope.get("subtype")}: '
                         f'{str(envelope.get("result"))[:500]}')
    if kind == 'agy' and envelope.get('status') not in (None, 'SUCCESS'):
        raise AgentError(f'agy status {envelope.get("status")}: {str(envelope.get("response"))[:500]}')
    output = envelope.get('structured_output')
    if output is None:
        raise AgentError(f'{kind} returned no structured_output')
    return output


class AgentRunner:
    def __init__(self, log_dir, timeout, run=subprocess.run, env=None):
        self.log_dir = Path(log_dir)
        self.timeout = timeout
        self._run = run
        self._env = env
        self._versions = {}

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
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stem = self.log_dir / f'{time.strftime("%Y%m%dT%H%M%S")}-{label}-{agent.key}'
        out_file = stem.with_suffix('.last.json')
        argv, stdin = build_command(agent, mode, prompt, schema_file=schema_path(schema_name),
                                    out_file=out_file)
        stem.with_suffix('.prompt.md').write_text(prompt, encoding='utf-8')
        log = stem.with_suffix('.log')
        kwargs = {'env': self._env} if self._env is not None else {}
        # Its own process group: a timeout also stops the commands the CLI left in the background.
        run = run_in_group if self._run is subprocess.run else self._run
        try:
            done = run(argv, input=stdin, cwd=workdir, capture_output=True, text=True,
                       timeout=self.timeout, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f'{agent.key} timed out after {self.timeout}s', log) from exc
        except OSError as exc:
            raise AgentError(f'{agent.key} could not start: {exc}', log) from exc
        log.write_text(f'$ {argv[0]} ({mode})\nexit {done.returncode}\n--- stdout\n{done.stdout}'
                       f'\n--- stderr\n{done.stderr}', encoding='utf-8')
        if done.returncode != 0:
            raise AgentError(f'{agent.key} exited with {done.returncode}', log)
        try:
            output = extract_output(agent.kind, done.stdout, out_file)
            return validate(output, load_schema(schema_name))
        except AgentError as exc:
            exc.log = log
            raise
        except (SchemaError, json.JSONDecodeError) as exc:
            raise AgentError(f'{agent.key} output does not match {schema_name}: {exc}', log) from exc
