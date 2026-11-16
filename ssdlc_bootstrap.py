"""Read-only, explicitly requested greenfield eligibility and registered policy.

Pre-development authority is persisted separately from the first-checkpoint
exemption. ``PREPARATION`` is written once, before the absent-baseline contract,
and lets recon/PO/design resume after verification. ``RESERVATION`` is created
exclusively before the worktree, the task journal or any developer dispatch:
once it exists, in any state, the exemption is spent for every later invocation.
"""
import configparser
import os
import shlex
import tomllib
import copy
import fnmatch
import json
import logging
import re
from pathlib import Path

from ssdlc_tasks import PlanError, TaskError, TaskJournal, atomic_json, confined, digest, file_hash, git, validate_document

CODES = ('profile_invalid', 'not_registered', 'existing_tests', 'dirty_tree',
         'not_initial_commit', 'tree_mismatch', 'policy_conflict', 'interrupted', 'window_spent',
         'preparation_invalid', 'controller_changed', 'policy_changed', 'inputs_changed', 'limits_changed')
PREPARATION = '00-recon/bootstrap-preparation.json'
RESERVATION = '00-recon/bootstrap-first-attempt.json'
PREPARATION_FORMAT = 'bootstrap-preparation-v1'
RESERVATION_FORMAT = 'bootstrap-first-attempt-v1'
# Any of these means the first development attempt may have started.
DEVELOPMENT_EVIDENCE = (RESERVATION, 'worktree', '03-dev/tasks/task-journal.jsonl', '03-dev/implementation.diff')
PREPARATION_KEYS = {'schema_version', 'format', 'ticket', 'status', 'contract_sha256', 'binding',
                    'controller', 'limits_policy_sha256'}
COUNTERS = {'bootstrap_eligible': 0, 'bootstrap_ineligible': 0}


class BootstrapError(PlanError):
    def __init__(self, code):
        self.code = code
        super().__init__('bootstrap_ineligible: ' + code)


PROFILES = {
    'python-unittest': {'argv': ['python3', '-m', 'unittest', 'discover', '-s', 'tests'], 'window_tasks': 1},
    'python-pytest': {'argv': ['python3', '-m', 'pytest', 'tests'], 'window_tasks': 1},
}


def _log_run_id(value):
    text = str(value)
    return text if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', text) else '-'


def _ineligible(code, run_id='-'):
    COUNTERS['bootstrap_ineligible'] += 1
    logging.getLogger(__name__).warning('bootstrap stage=eligibility decision=ineligible reason=%s run=%s txn=- preview=- tasks=-', code, _log_run_id(run_id))
    return BootstrapError(code)


def load_profile(repo, *, run_id='-'):
    path = Path(repo) / '.ssdlc/bootstrap-profile.json'
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 4096:
            raise BootstrapError('profile_invalid')
        profile = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(profile, dict) and profile.get('profile_id') not in PROFILES:
            raise BootstrapError('not_registered')
        validate_document('bootstrap-profile', profile)
        return profile
    except BootstrapError as exc:
        raise _ineligible(exc.code, run_id) from None
    except (OSError, ValueError, PlanError):
        raise _ineligible('profile_invalid', run_id) from None


def policy_sha256(profile_id):
    if profile_id not in PROFILES:
        raise BootstrapError('not_registered')
    return digest(PROFILES[profile_id])


def profile_checks(profile_id):
    policy_sha256(profile_id)
    return [dict(check_id='CHECK-TESTS', argv=copy.deepcopy(PROFILES[profile_id]['argv']),
                 cwd='.', timeout_sec=300, kind='test', required=True, candidate_proof=True)]


def require_unproven_run_is_current(root, contract=None, *, first_invocation=False):
    """A sealed absent-baseline contract cannot renew its first-run exemption."""
    root = Path(root)
    if contract is None:
        try:
            path = confined(root, '00-recon/quality-contract.json', protect=False)
            if not path.exists():
                return
            contract = json.loads(path.read_text())
        except (OSError, ValueError, TaskError):
            raise PlanError('Persisted quality contract is unreadable') from None
    if not isinstance(contract, dict) or not contract.get('bootstrap'):
        return
    if first_invocation:
        return
    if proven(root):
        return
    require_preparation_resumable(root, contract)


def proven(root):
    try:
        path = confined(Path(root), '03-dev/tasks/task-journal.jsonl', protect=False)
        raw = path.read_bytes() if path.exists() else b''
        complete = raw[:raw.rfind(b'\n') + 1]
        state, _, _ = TaskJournal._replay(complete)
        return bool(state and state.get('bootstrap', {}).get('status') == 'PROVEN')
    except (OSError, ValueError, TaskError):
        return False


def fault(point):
    """Test hook at each persistence boundary of the preparation protocol."""


def _exists(root, name):
    path = Path(root) / name
    return path.exists() or path.is_symlink()


def development_started(root):
    """A missing journal is never proof that no attempt started."""
    return any(_exists(root, name) for name in DEVELOPMENT_EVIDENCE)


def preparation_record(ticket, contract, controller, limits_policy_sha256):
    return dict(schema_version='1.0', format=PREPARATION_FORMAT, ticket=ticket, status='PREPARING',
                contract_sha256=digest(contract), binding=copy.deepcopy(contract['bootstrap']),
                controller=copy.deepcopy(controller), limits_policy_sha256=limits_policy_sha256)


def read_preparation(root):
    """Return the persisted record, None when absent; refuse anything unrecognised."""
    root = Path(root)
    if not _exists(root, PREPARATION):
        return None
    try:
        path = confined(root, PREPARATION, protect=False)
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 65536:
            raise ValueError('unsafe preparation record')
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TaskError):
        raise _ineligible('preparation_invalid', root.name) from None
    if (not isinstance(record, dict) or set(record) != PREPARATION_KEYS
            or record['schema_version'] != '1.0' or record['format'] != PREPARATION_FORMAT
            or record['status'] != 'PREPARING' or not isinstance(record['binding'], dict)
            or not isinstance(record['controller'], dict)):
        raise _ineligible('preparation_invalid', root.name)
    return record


def require_preparation_resumable(root, contract):
    """Read-only admission of a sealed contract whose first attempt never started."""
    root = Path(root)
    record = read_preparation(root)
    if record is None:
        # Historical contracts and interrupted exemptions are never converted.
        raise _ineligible('interrupted', root.name)
    if development_started(root):
        raise _ineligible('window_spent', root.name)
    if record['contract_sha256'] != digest(contract) or record['binding'] != contract.get('bootstrap'):
        raise _ineligible('preparation_invalid', root.name)
    return record


def seal_preparation(root, record):
    """Write the record once, before the contract; an interrupted seal rolls forward only if identical."""
    root = Path(root)
    if development_started(root):
        raise _ineligible('window_spent', root.name)
    existing = read_preparation(root)
    if existing is not None:
        if existing != record:
            raise _ineligible('inputs_changed', root.name)
        return
    fault('before_preparation_record')
    atomic_json(confined(root, PREPARATION, protect=False), record)
    fault('after_preparation_record')


def recover_unregistered_contract(root, contract):
    """A contract written after its preparation record but before registration."""
    try:
        require_preparation_resumable(root, contract)
    except BootstrapError:
        return False
    return True


def verify_preparation(root, repo, contract, *, ticket, base_commit, controller,
                       limits_policy_sha256, artifacts):
    """Full check before resuming preparation or reserving the first attempt."""
    root = Path(root)
    record = require_preparation_resumable(root, contract)
    binding = contract['bootstrap']
    if record['ticket'] != ticket or binding.get('initial_commit') != base_commit:
        raise _ineligible('preparation_invalid', ticket)
    if record['controller'] != controller:
        raise _ineligible('controller_changed', ticket)
    if binding.get('profile_id') not in PROFILES or policy_sha256(binding['profile_id']) != binding.get('policy_sha256'):
        raise _ineligible('policy_changed', ticket)
    if record['limits_policy_sha256'] != limits_policy_sha256:
        raise _ineligible('limits_changed', ticket)
    for name, meta in sorted(artifacts.items()):
        if not name.startswith(('00-recon/', '01-po/', '02-design/')):
            continue
        try:
            path = confined(root, meta['path'], protect=False)
            if path.is_symlink() or not path.is_file() or file_hash(path) != meta['sha256']:
                raise ValueError(name)
        except (OSError, ValueError, KeyError, TypeError, TaskError):
            raise _ineligible('inputs_changed', ticket) from None
    profile = load_profile(repo, run_id=ticket)
    if profile is None:
        raise _ineligible('profile_invalid', ticket)
    if decide(repo, base_commit, profile, run_id=ticket) != binding:
        raise _ineligible('inputs_changed', ticket)
    return record


def reserve_first_attempt(root, ticket):
    """Exclusively claim the one exemption window before any development write."""
    root = Path(root)
    record = read_preparation(root)
    if record is None:
        raise _ineligible('interrupted', ticket)
    path = confined(root, RESERVATION, protect=False)
    data = json.dumps(dict(schema_version='1.0', format=RESERVATION_FORMAT, ticket=ticket,
                           contract_sha256=record['contract_sha256'],
                           preparation_sha256=file_hash(root / PREPARATION)), indent=2).encode() + b'\n'
    fault('before_reservation')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise _ineligible('window_spent', ticket) from None
    try:
        # The claim is the file's existence; a torn body still refuses renewal.
        fault('reservation_created')
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    fault('after_reservation')
    logging.getLogger(__name__).info('bootstrap stage=reservation decision=reserved reason=first_attempt run=%s txn=- preview=- tasks=-', _log_run_id(ticket))


def decide(repo, base_commit, profile, *, run_id='-'):
    repo = Path(repo)
    try:
        validate_document('bootstrap-profile', profile)
        policy_hash = policy_sha256(profile['profile_id'])
        if (git(repo, 'rev-parse', '--is-shallow-repository') == 'true'
                or git(repo, 'rev-list', '--count', 'HEAD') != '1'
                or git(repo, 'rev-list', '--max-parents=0', 'HEAD') != base_commit
                or git(repo, 'rev-parse', 'HEAD') != base_commit):
            raise BootstrapError('not_initial_commit')
        if git(repo, 'status', '--porcelain') or git(repo, 'ls-files', '-o', '--exclude-standard'):
            raise BootstrapError('dirty_tree')
        tree = git(repo, 'rev-parse', 'HEAD^{tree}')
        if tree != git(repo, 'rev-parse', base_commit + '^{tree}'):
            raise BootstrapError('tree_mismatch')
        if (repo / '.ssdlc/quality-contract.json').exists():
            raise BootstrapError('policy_conflict')
        from orchestrator import _discover_checks
        paths = git(repo, 'ls-files', '-z').split('\x00')
        patterns = ['test*.py', '*_test.*', '*.test.*', '*.spec.*', '*Test.java', '*Tests.java', '*_spec.rb']
        # Honour pytest's configured discovery names without executing target code.
        for name, section in [('pytest.ini', 'pytest'), ('.pytest.ini', 'pytest'),
                              ('tox.ini', 'pytest'), ('setup.cfg', 'tool:pytest')]:
            path = repo / name
            if path.is_file():
                config = configparser.ConfigParser(interpolation=None)
                config.read_string(path.read_text())
                if config.has_option(section, 'python_files'):
                    patterns.extend(shlex.split(config.get(section, 'python_files')))
        pyproject = repo / 'pyproject.toml'
        if pyproject.is_file():
            values = tomllib.loads(pyproject.read_text()).get('tool', {}).get('pytest', {}).get('ini_options', {}).get('python_files', [])
            patterns.extend(shlex.split(values) if isinstance(values, str) else values)
        discovered = _discover_checks(repo)
        if any(c['kind'] == 'test' for c in discovered) or any(fnmatch.fnmatch(Path(p).name, pattern) for p in paths for pattern in patterns):
            raise BootstrapError('existing_tests')
        if discovered:
            raise BootstrapError('policy_conflict')
        result = dict(profile_id=profile['profile_id'], profile_sha256=digest(profile),
                      policy_sha256=policy_hash, initial_commit=base_commit, initial_tree=tree,
                      window_tasks=1, baseline_status='absent')
    except (PlanError, OSError, ValueError, TypeError, AttributeError, configparser.Error) as exc:
        code = exc.code if isinstance(exc, BootstrapError) else 'profile_invalid'
        raise _ineligible(code, run_id) from None
    COUNTERS['bootstrap_eligible'] += 1
    logging.getLogger(__name__).info('bootstrap stage=eligibility decision=eligible reason=eligible run=%s txn=- preview=- tasks=-', _log_run_id(run_id))
    return result


def positive_test_count(stdout, stderr):
    """Use the final runner summary, excluding tests reported as skipped."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', stdout + '\n' + stderr)
    patterns = (
        ('unittest', r'(?m)^Ran (\d{1,12}) tests?\b'),
        ('pytest', r'(?m)^\s*=*\s*(\d{1,12}) passed\b'),
        ('jest', r'(?m)^\s*Tests:?\s+(\d{1,12})\b[^\r\n]*'),
        ('tap', r'(?m)^# tests (\d{1,12})\b'),
        ('mocha', r'(?m)^\s*(\d{1,12}) passing\b'),
        ('pytest', r'(?m)^collected (0) items?\b'),
        ('cargo', r'(?m)^test result: ok\. (\d{1,12}) passed;[^\r\n]*'),
        ('maven', r'(?m)^(?:\[INFO\]\s*)?Tests run: (\d{1,12}), Failures: \d+, Errors: \d+, Skipped: \d+[^\r\n]*'),
        ('rspec', r'(?m)^\s*(\d{1,12}) examples?, \d+ failures?[^\r\n]*'),
    )
    counts = [(match.start(), int(match.group(1)), kind, match.end()) for kind, pattern in patterns
              for match in re.finditer(pattern, text)]
    empty = [match.start() for match in re.finditer(r'(?m)^=*\s*no tests ran\b', text)]
    if empty and (not counts or max(empty) > max(row[0] for row in counts)):
        return 0
    if not counts:
        # go test -json identifies individual executed tests, even when no
        # package summary contains a test count. Ignore package-only passes.
        events = []
        for line in text.splitlines():
            if not line.startswith('{'):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and 'Action' in row and 'Package' in row:
                events.append(row)
        if events:
            return sum(e.get('Action') == 'pass' and bool(e.get('Test')) for e in events)
        return None
    position, total, kind, end = max(counts, key=lambda item: item[0])
    tail = text[end:]
    if kind == 'cargo':
        return sum(n for _, n, k, _ in counts if k == 'cargo')
    if kind in ('maven', 'rspec'):
        pattern = (r'(?:Failures: |Errors: |Skipped: )(\d+)' if kind == 'maven'
                   else r'(\d+) (?:pending|failures?)')
        excluded = sum(int(m.group(1)) for m in re.finditer(pattern, text[position:end]))
        return max(0, total - excluded)
    if kind == 'unittest':
        summary = re.search(r'(?m)^(?:OK|FAILED)\s*\(([^\r\n)]*)\)', tail)
        excluded = sum(int(m.group(1)) for m in re.finditer(
            r'\b(?:skipped|expected failures|failures|errors|unexpected successes)=(\d{1,12})\b',
            summary.group(1) if summary else ''))
        return max(0, total - excluded)
    if kind == 'jest':
        summary = text[position:end]
        passed = re.search(r'\b(\d{1,12}) passed\b', summary)
        if passed:
            return int(passed.group(1))
        return 0 if re.search(r'\b(?:skipped|pending|todo|failed)\b', summary) else None
    if kind == 'tap':
        passed = re.search(r'(?m)^# pass (\d{1,12})\b', tail)
        if passed:
            return min(total, int(passed.group(1)))
        skipped = sum(int(match.group(1)) for match in re.finditer(
            r'(?m)^# (?:skip(?:ped)?|cancelled|todo) (\d{1,12})\b', tail))
        return max(0, total - skipped)
    return total
