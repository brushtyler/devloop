"""Host-owned preparation receipts and reversible discard for unselected imports."""
import json
import os
from pathlib import Path
import shutil

from ssdlc_tasks import atomic_json, file_hash, git, ticket_lock, confined
from ssdlc_transitions import TransitionError, _regular, source_binding, sync_tree


def receipt_path(ticket, identity, namespace="continuations"):
    if namespace not in ('continuations', 'execution'):
        raise TransitionError('Unknown preparation namespace')
    return Path(ticket) / namespace / 'preparations' / (identity + '.json')


def register(ticket, repo, source, identity, base, *, namespace="continuations"):
    record = {'schema_version': 1, 'namespace': namespace, 'transition_id': identity, 'repo': str(Path(repo).resolve()),
              'source_root': str(Path(source).resolve()), 'ticket_root': str(Path(ticket).resolve()), 'base_commit': base,
              'preparation': namespace + '/prepared-' + identity,
              'runtime': ('runtime-generations/' if namespace == 'continuations' else 'execution-runtimes/') + identity,
              'branch': f'ssdlc/{Path(source).name}/continuation-{identity}',
              'status': 'PREPARING', 'worktree_created': False}
    atomic_json(receipt_path(ticket, identity, namespace), record)
    return record


def update(ticket, identity, *, namespace="continuations", **fields):
    path = receipt_path(ticket, identity, namespace)
    record = json.loads(_regular(path.parent, path.name).read_text())
    record.update(fields)
    atomic_json(path, record)
    return record


def pending(ticket, identity, *, namespace="continuations"):
    record = update(ticket, identity, namespace=namespace, status='PREPARED')
    atomic_json(Path(ticket) / namespace / 'pending.json',
                {'schema_version': 1, 'transition_id': identity,
                 'receipt_sha256': file_hash(receipt_path(ticket, identity, namespace))})


def clear_pending(ticket, identity, *, namespace="continuations"):
    path = Path(ticket) / namespace / 'pending.json'
    if path.exists():
        value = json.loads(_regular(path.parent, path.name).read_text())
        if value.get('transition_id') != identity:
            raise TransitionError('Another preparation owns the pending fence')
        path.unlink()
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)


def unfinished(ticket, *, namespace="continuations"):
    root = Path(ticket) / namespace
    records = []
    for path in (root / 'preparations').glob('*.json'):
        record = json.loads(_regular(path.parent, path.name).read_text())
        if record['status'] not in ('ABORTED', 'ACTIVE'):
            records.append(record['transition_id'])
    known = {p.stem for p in (root / 'preparations').glob('*.json')}
    active = json.loads(_regular(root, 'active.json').read_text())['transition_id'] if (root / 'active.json').exists() else None
    runtimes = Path(ticket) / ('runtime-generations' if namespace == 'continuations' else 'execution-runtimes')
    if runtimes.exists() and any(p.name not in known and p.name != active for p in runtimes.iterdir() if p.is_dir()):
        raise TransitionError('Untracked historical runtime needs explicit ownership reconciliation')
    if any(p.name.removeprefix('prepared-') not in known for p in root.glob('prepared-*')):
        raise TransitionError('Untracked historical preparation needs explicit ownership reconciliation')
    return records


def _archive_tree(source, target, skip=()):
    """Preserve bytes before cleanup; never follow candidate-provided symlinks."""
    if source.is_symlink() or target.is_symlink(): raise TransitionError('Unsafe archive root')
    if not source.exists(): return
    for path in source.rglob('*'):
        relative = path.relative_to(source)
        if any(part in skip for part in relative.parts): continue
        if any((source / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            raise TransitionError('Unsafe staged archive entry; cleanup refused')
        if path.is_file():
            data = path.read_bytes(); destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() != data:
                raise TransitionError('Existing discard archive differs; preserve both for reconciliation')
            destination.write_bytes(data)
    if target.exists(): sync_tree(target)


def discard(ticket, identity, *, writers_quiescent, namespace="continuations"):
    """Discard only our recorded, unselected resources, never the source run."""
    import re
    if not re.fullmatch(r'[a-f0-9]{32}', identity): raise TransitionError('Invalid preparation identity')
    ticket = Path(ticket).resolve()
    path = receipt_path(ticket, identity, namespace)
    with ticket_lock(ticket / 'ticket.lock'):
        record = json.loads(_regular(path.parent, path.name).read_text())
        selected = ticket / namespace / 'active.json'
        if selected.exists():
            active = json.loads(_regular(selected.parent, selected.name).read_text())
            if active['transition_id'] == identity:
                raise TransitionError('An active generation cannot be discarded')
        if record['status'] == 'ACTIVE':
            raise TransitionError('An active generation cannot be discarded')
        if record['status'] == 'ABORTED': return record
        if record['transition_id'] != identity or record['runtime'] != ('runtime-generations/' if namespace == 'continuations' else 'execution-runtimes/') + identity or record['preparation'] != namespace + '/prepared-' + identity:
            raise TransitionError('Preparation ownership receipt changed')
        if not writers_quiescent(record):
            raise TransitionError('Preparation writers are not proven quiescent; no cleanup')
        runtime = confined(ticket, record['runtime'], protect=False)
        preparation = confined(ticket, record['preparation'], protect=False)
        if runtime.resolve() == Path(record['source_root']).resolve():
            raise TransitionError('Discard cannot remove the historical source')
        archive = ticket / namespace / 'aborted' / identity
        archive.mkdir(parents=True, exist_ok=True, mode=0o700)
        _archive_tree(runtime, archive / 'runtime', skip=('worktree',))
        _archive_tree(preparation, archive / 'preparation')
        worktree = runtime / 'worktree'
        repo = Path(record['repo'])
        if worktree.exists():
            # A host crash can follow checkpoint publication but precede the
            # final preparation receipt. Reconcile only a journal-proven commit
            # on the exact owned branch, preserving source before removal.
            head = git(worktree, 'rev-parse', 'HEAD')
            if namespace == 'execution' and head != record.get('staged_head', record['base_commit']):
                from ssdlc_tasks import TaskJournal
                journal = TaskJournal(runtime / '03-dev/tasks').peek()
                if (not journal or journal.get('expected_head') != head or not any(
                        row.get('status') == 'COMPLETED' and row.get('commit') == head
                        for row in journal['tasks'].values())
                        or git(worktree, 'merge-base', head, record['base_commit']) != record['base_commit']):
                    raise TransitionError('Staged checkpoint ownership is unproven; cleanup refused')
                record = update(ticket, identity, namespace=namespace, staged_head=head)
            from ssdlc_recovery import require_unlocked_git
            require_unlocked_git(worktree)
            entries = git(repo, 'worktree', 'list', '--porcelain').split('\n\n')
            expected = [block for block in entries if 'worktree ' + str(worktree) in block.splitlines()]
            if (len(expected) != 1 or 'branch refs/heads/' + record['branch'] not in expected[0].splitlines()
                    or git(worktree, 'rev-parse', 'HEAD') != record.get('staged_head', record['base_commit'])):
                raise TransitionError('Staged worktree identity changed; cleanup refused')
            _archive_tree(worktree, archive / 'source', skip=('.git',))
            update(ticket, identity, namespace=namespace, status='ABORTING')
            git(repo, 'worktree', 'remove', '--force', str(worktree))
        # A branch can survive a crash immediately after worktree creation/removal.
        branches = git(repo, 'branch', '--list', record['branch']).strip()
        if branches:
            if git(repo, 'rev-parse', 'refs/heads/' + record['branch']) != record.get('staged_head', record['base_commit']):
                raise TransitionError('Staged branch changed; cleanup refused')
            git(repo, 'branch', '-D', '--', record['branch'])
        if runtime.exists(): shutil.rmtree(runtime)
        if preparation.exists(): shutil.rmtree(preparation)
        record = update(ticket, identity, namespace=namespace, status='ABORTED', archive=str(archive.relative_to(ticket)))
        clear_pending(ticket, identity, namespace=namespace)
        return record


def _atomic_bytes(path, data):
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def _inventory_diff(before, after):
    """Existing source must stay stable; scratch additions are listed, not refused."""
    return {'modified': sorted(p for p in before if p in after and after[p] != before[p]),
            'deleted': sorted(p for p in before if p not in after),
            'added': sorted(p for p in after if p not in before)}


def _inspect(read, errors, key):
    """Post-run reads may fail on verifier damage; record the failure as evidence."""
    from ssdlc_candidates import CandidateError
    try:
        return read()
    except CandidateError as exc:
        errors[key] = str(exc)
    except OSError as exc:
        # strerror only: the message must not carry host paths.
        errors[key] = type(exc).__name__ + ': ' + (exc.strerror or 'unreadable')
    return None


def _metadata_diff(worker, before, errors):
    """Diff checkout metadata against its pre-run identity; empty means unchanged.

    Unreadable or deleted metadata counts as changed: the failure goes to
    ``errors`` and the readable part of the comparison to the diff.
    """
    from ssdlc_candidates import _git, git_metadata
    directory = Path(worker) / '.git'
    after = _inspect(lambda: git_metadata(worker), errors, 'metadata')
    if after is None and not os.path.lexists(directory):
        after = {}  # deleted metadata: every recorded file is gone
    identity = {'head': None, 'tree': None}
    if directory.is_dir() and not directory.is_symlink():
        identity['head'] = _inspect(lambda: _git(worker, 'rev-parse', 'HEAD'), errors, 'head')
        identity['tree'] = _inspect(lambda: _git(worker, 'write-tree'), errors, 'tree')
    diff = {} if after is None else _inventory_diff(before['metadata'], after)
    changed = {key: {'before': before[key], 'after': identity[key]} for key in identity
               if identity[key] != before[key]}
    if changed:
        diff['identity'] = changed
    failed = any(key in errors for key in ('metadata', 'head', 'tree'))
    if not failed and not any(diff.values()):
        return {}
    return diff


def run_baseline_check(verifier, check, worker, cwd, runtime, log_ref, preparation):
    """Run one baseline check and record it durably before any outcome decision.

    The sanitized log is written under the generation runtime at ``log_ref``;
    exit code, duration, classification and, for mutations, the source and
    metadata diffs are written to ``baseline/<check_id>.json`` inside the
    preparation. Both are archived by native discard. Post-run inspections that
    fail on verifier damage are recorded as mutations, never raised past the
    evidence. The caller decides only from the returned record.
    """
    import subprocess
    import time
    from ssdlc_candidates import git_identity, inventory
    from ssdlc_tasks import redact_sensitive, test_suite_is_empty
    before, metadata = inventory(worker), git_identity(worker)
    started = time.monotonic()
    try:
        out, err, code = verifier(check['argv'], cwd, check['timeout_sec'], worker)
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        # Keep partial output as evidence; a timeout never counts as a pass.
        out, err = exc.output or '', exc.stderr or ''
        out = out.decode('utf-8', errors='replace') if isinstance(out, bytes) else out
        err = err.decode('utf-8', errors='replace') if isinstance(err, bytes) else err
        err += f'\nVerification timeout: {exc}'
        code, timed_out = 124, True
    duration = round(time.monotonic() - started, 3)
    log = confined(runtime, log_ref, protect=False)
    # Same boundary as check logs: host-isolated output, plus credential redaction.
    # Written before any post-run inspection, which the verifier may have broken.
    _atomic_bytes(log, redact_sensitive(out + '\n' + err).encode())
    log_sha256 = file_hash(log)
    errors = {}
    after = _inspect(lambda: inventory(worker), errors, 'source')
    diff = None if after is None else _inventory_diff(before, after)
    metadata_diff = _metadata_diff(worker, metadata, errors)
    metadata_changed = bool(metadata_diff)
    empty = not timed_out and test_suite_is_empty(out, err)
    if diff is None or diff['modified'] or diff['deleted']:
        classification = 'SOURCE_CHANGED'
    elif metadata_changed:
        classification = 'METADATA_CHANGED'
    elif timed_out:
        classification = 'TIMEOUT'
    elif code:
        classification = 'FAILED'
    elif empty:
        classification = 'EMPTY_SUITE'
    else:
        classification = 'PASSED'
    record = {'schema_version': 1, 'check_id': check['check_id'], 'exit_code': code,
              'empty_suite': empty, 'duration_sec': duration, 'classification': classification,
              'log_ref': log_ref, 'log_sha256': log_sha256, 'metadata_changed': metadata_changed}
    if diff is not None and (classification == 'SOURCE_CHANGED' or diff['added']):
        record['inventory_diff'] = diff
    if metadata_diff:
        record['metadata_diff'] = metadata_diff
    if errors:
        record['inspection_errors'] = errors
    record_ref = 'baseline/' + check['check_id'] + '.json'
    atomic_json(Path(preparation) / record_ref, record)
    record['record_ref'], record['record_sha256'] = record_ref, file_hash(Path(preparation) / record_ref)
    return record


def record_baseline_refusal(ticket, identity, preparation, record, message, *, namespace="continuations"):
    """Persist the refusal linked to its recorded evidence, then let the caller raise."""
    refusal = {'schema_version': 1, 'transition_id': identity, 'namespace': namespace,
               'reason': record['classification'], 'message': message, 'check_id': record['check_id'],
               'exit_code': record['exit_code'], 'duration_sec': record['duration_sec'],
               'log': {'root': 'runtime', 'ref': record['log_ref'], 'sha256': record['log_sha256']},
               'check_record': {'root': 'preparation', 'ref': record['record_ref'], 'sha256': record['record_sha256']},
               'generation_selected': False, 'model_dispatches': 0}
    for key in ('inventory_diff', 'metadata_diff', 'inspection_errors'):
        if key in record:
            refusal[key] = record[key]
    path = Path(preparation) / 'refusal.json'
    atomic_json(path, refusal)
    update(ticket, identity, namespace=namespace, refusal_ref='refusal.json', refusal_sha256=file_hash(path))
    return refusal


def docker_quiescent(record):
    """Observe only mount ownership; never signal unrelated containers."""
    import subprocess
    roots = [str((Path(record['ticket_root']) / record[k]).resolve()) for k in ('runtime', 'preparation')]
    roots.append(str(Path(record['source_root']).resolve()))
    try:
        listed = subprocess.run(['docker', 'ps', '-aq'], capture_output=True, text=True, timeout=15, check=True)
        identities = listed.stdout.split()
        if not identities: return True
        data = subprocess.run(['docker', 'inspect', *identities], capture_output=True, text=True, timeout=30, check=True)
        containers = json.loads(data.stdout)
        for container in containers:
            state = container['State']
            if not isinstance(state.get('Running'), bool): return False
            if not state['Running']: continue
            for mount in container['Mounts']:
                source = str(Path(mount['Source']).resolve())
                if mount.get('RW') and any(source == root or source.startswith(root + '/') or root.startswith(source.rstrip('/') + '/') for root in roots):
                    return False
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False
