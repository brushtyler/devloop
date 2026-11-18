"""Read-only ``WorkflowSnapshot`` of a run: generations, task DAG, feedback and metrics.

Part 1 of the ``workflow-status`` CLI (#46, #346). The snapshot never writes: it
opens no lock, repairs no journal tail, creates no directory and refreshes no
projection. Every file it reads is fingerprinted when read (content hash and the
identity of the open file); after the collection the same files are read again.
If one changed, the collection is repeated, at most ``MAX_ATTEMPTS`` times, and
then the snapshot is ``TORN`` with no data. A coherent snapshot reflects the
files as they all were at one instant between the collection and the check.

Metrics are only the ones already persisted. Each declares its coverage
(``complete``, ``partial`` or ``none``); a value that is not observable is
``null`` with the reason. No timestamp is ever derived from a file's mtime.

Part 2 (#347) adds the ``status``: liveness observed in the kernel, its coverage, the
activity, the native classification of a stop and the next action
(``ssdlc_workflow_liveness``). Part 3 (#348) adds the ``projection`` of the remaining cost, computed by the pure
``ssdlc_workflow_projection.project`` from the data read here, frozen limits included.
Part 4 (#349) adds the ``log``: the last persisted log, chosen from the coherent data,
and its sanitized tail (at most ``TAIL_BYTES``), read after the check; the command
itself is ``ssdlc_workflow_cli``.
"""
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import stat
import time

SCHEMA = 'ssdlc.workflow-snapshot'
SCHEMA_VERSION = 1
MAX_ATTEMPTS = 3

COHERENT, TORN, UNVERIFIABLE = 'COHERENT', 'TORN', 'UNVERIFIABLE'
COMPLETE, PARTIAL, NONE = 'complete', 'partial', 'none'

TICKET = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
STORES = (('execution', 'execution', 'execution-runtimes'),
          ('continuation', 'continuations', 'runtime-generations'))
JOURNAL = '03-dev/tasks/task-journal.jsonl'
PLAN = '02-design/plan.json'
FEEDBACK_FILES = ('feedback/authority.json', 'feedback/task-journal.jsonl')
LIMITS_POLICY, LIMITS_JOURNAL = 'limits/policy.json', 'limits/task-journal.jsonl'
LIMITS_PENDING = 'limits/extension-pending.json'

# The last persisted log: references the task journal records, else logs registered as artifacts.
LOG_KEYS = ('log_ref', 'developer_log_ref', 'output_ref')
SAFE_REF = re.compile(r'[A-Za-z0-9_./-]{1,200}')
TAIL_BYTES = 8192
LOG_GAP = ("only logs the task journal references or the run registers as artifacts are seen; "
           "the detached controller's stdout and stderr are not kept")

NOT_OBSERVABLE = {
    'task_elapsed_sec': 'task journal events carry no timestamp; file mtimes are never used',
    'phase_elapsed_sec': 'phase start and end times are not persisted; file mtimes are never used',
    'check_duration_sec': 'ordinary checks record only their outcome; a duration is persisted only for timeouts',
    'operator_wait_sec': 'the time spent waiting for the operator is not persisted',
}


class Unverifiable(Exception):
    """Data that is stable on disk but cannot be read or verified."""


class Changed(Exception):
    """A file changed while it was being read."""


def _open_file(ticket, relative):
    """A read-only descriptor of ``<ticket>/<relative>`` and its ``fstat``; ``(None, None)`` if absent.

    No path component may be a symlink: ``O_NOFOLLOW`` on each one, from the
    ticket root down. Anything that is not a regular file is refused. The last
    component is opened with ``O_NONBLOCK``, so a FIFO without a writer or a
    device is refused by ``fstat`` instead of blocking the open.
    """
    parts = Path(relative).parts
    if (not parts or Path(relative).is_absolute() or '..' in parts
            or any(ord(c) < 32 for c in relative) or '\\' in relative):
        raise Unverifiable(f'unsafe path {relative!r}')
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    try:
        fd = os.open(ticket, flags | os.O_DIRECTORY)
    except OSError as exc:
        raise Unverifiable(f'ticket root unreadable: {type(exc).__name__}') from None
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            try:
                final = os.O_NONBLOCK | getattr(os, 'O_NOCTTY', 0)
                child = os.open(part, flags | (final if last else os.O_DIRECTORY), dir_fd=fd)
            except FileNotFoundError:
                os.close(fd)
                return None, None
            except OSError as exc:
                raise Unverifiable(f'{relative}: {type(exc).__name__}') from None
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Unverifiable(f'{relative}: not a regular file')
    except BaseException:
        os.close(fd)
        raise
    return fd, info


class Reader:
    """Reads files of the ticket without writing, recording what each read saw."""

    def __init__(self, ticket):
        self.ticket = ticket
        self.seen = {}

    def _open(self, relative):
        """``(fingerprint, bytes)``; ``(None, None)`` when the file does not exist.

        The fingerprint is the content hash with the identity of the open file, so
        an atomic replace with the same content is still seen.
        """
        fd, info = _open_file(self.ticket, relative)
        if fd is None:
            return None, None
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b''.join(chunks)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns) or len(data) != info.st_size:
            raise Changed(relative)
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        return dict(sha256=hashlib.sha256(data).hexdigest(), identity=identity), data

    def read(self, relative):
        """The bytes of a ticket file, or None if absent; the first read is the one kept."""
        fingerprint, data = self._open(relative)
        if relative in self.seen and self.seen[relative] != fingerprint:
            raise Changed(relative)
        self.seen[relative] = fingerprint
        return data

    def document(self, relative):
        """``(present, value)`` of a JSON file: a file holding ``null`` is present, not absent."""
        data = self.read(relative)
        if data is None:
            return False, None
        try:
            return True, json.loads(data)
        except ValueError:
            raise Unverifiable(f'{relative}: invalid JSON') from None

    def note(self, relative):
        """Fingerprint a file read by another, read-only, reader of the engine."""
        self.read(relative)

    def observe(self, path):
        """``observe`` hook of the engine's readers: fingerprint each absolute path they read."""
        try:
            relative = Path(path).relative_to(self.ticket)
        except ValueError:
            raise Unverifiable(f'path outside the ticket: {path}') from None
        self.note(relative.as_posix())

    def stable(self):
        """Whether every file read so far still has the fingerprint it had when read."""
        for relative, fingerprint in self.seen.items():
            try:
                if self._open(relative)[0] != fingerprint:
                    return False
            except (Changed, Unverifiable):
                return False
        return True

    def sources(self):
        return {relative: (fingerprint or {}).get('sha256') for relative, fingerprint in sorted(self.seen.items())}


def _transition_id(value):
    identity = value.get('transition_id') if isinstance(value, dict) else None
    if isinstance(identity, str) and re.fullmatch(r'[a-f0-9]{32}', identity):
        return identity
    return None


def _generation(reader):
    """The selected generation, with the same precedence as ``ssdlc_transitions.runtime_root``.

    A selector that does not validate is unverifiable, never a default. Pending
    selectors are reported, together with whether they fence dispatch.
    """
    from ssdlc_transitions import TransitionStore, TransitionError
    from ssdlc_tasks import TaskError
    selectors = {}
    for kind, store, _ in STORES:
        entry = {}
        for name in ('active', 'pending'):
            # Presence is the file's, never its value's: a selector holding ``null``
            # or any other structure without a generation is refused, not absent.
            present, value = reader.document(f'{store}/{name}.json')
            identity = _transition_id(value)
            if present and identity is None:
                raise Unverifiable(f'{store}/{name}.json: selector does not validate: it names no generation')
            entry.update({name: identity, f'{name}_present': present})
        selectors[kind] = entry
    execution, continuation = selectors['execution'], selectors['continuation']
    # The first published selector wins, as in ``ssdlc_fencing_proof.selected_generation``;
    # a pending one does not hide it, it fences dispatch (``dispatch_fenced``).
    selected = next((entry for entry in STORES if selectors[entry[0]]['active_present']), None)
    if execution['active_present']:
        fenced = execution['pending_present'] and execution['pending'] != execution['active']
    elif execution['pending_present']:
        fenced = True
    else:
        fenced = not continuation['active_present'] and continuation['pending_present']
    result = dict(selectors=selectors, dispatch_fenced=fenced, kind='base', transition_id=None, root='.')
    if selected is None:
        return result, '.', None
    kind, store, runtimes = selected
    try:
        # Every file ``load`` reads (pointer, intent, manifest, projection and sealed
        # artifacts) is fingerprinted, so the final check covers the generation too.
        projection = TransitionStore(reader.ticket / store, observe=reader.observe).load()
    except (TransitionError, TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        raise Unverifiable(f'{store} selector does not validate: {type(exc).__name__}: {exc}') from None
    identity = projection.get('transition_id') if isinstance(projection, dict) else None
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise Unverifiable(f'{store} selector names no generation')
    if identity != selectors[kind]['active']:
        raise Unverifiable(f'{store} projection names another generation')
    root = f'{runtimes}/{identity}'
    present, marker = reader.document(f'{root}/generation.json')
    if not present or not isinstance(marker, dict) or marker != projection.get('runtime_binding'):
        raise Unverifiable(f'{kind} generation binding changed')
    if kind == 'execution' and marker.get('model_dispatch_authorized') is not True:
        raise Unverifiable('execution generation is not authorized for dispatch')
    history = projection.get('ledger', [])
    if not isinstance(history, list):
        raise Unverifiable(f'{store} projection has no ledger list')
    result.update(kind=kind, transition_id=identity, root=root, ledger_prefix=len(history))
    return result, root, history


def _journal(reader, root):
    from ssdlc_tasks import TaskError, TaskJournal
    relative = JOURNAL if root == '.' else f'{root}/{JOURNAL}'
    data = reader.read(relative)
    if data is None:
        return None, None, (None, 0)
    boundary = data.rfind(b'\n') + 1
    try:
        state, sequence, sha = TaskJournal._replay(data[:boundary])
    except (TaskError, AttributeError, TypeError, KeyError, ValueError):
        # ``_replay`` lets some malformed records through as raw errors (a ``null`` line).
        raise Unverifiable('task journal hash chain does not verify') from None
    if state is not None and not isinstance(state, dict):
        raise Unverifiable('task journal state is not an object')
    head = dict(sequence=sequence, sha256=sha or None, interrupted_tail_bytes=len(data) - boundary)
    return head, state, _journal_log(data[:boundary], relative.rsplit('/', 1)[0])


def _log_refs(value, path=()):
    """Every ``(path, reference)`` to a log in a task journal state, in document order."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in LOG_KEYS and isinstance(item, str):
                yield path + (key,), item
            else:
                yield from _log_refs(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _log_refs(item, path + (index,))


def _safe_ref(ref):
    parts = ref.split('/')
    return bool(SAFE_REF.fullmatch(ref)) and '..' not in parts and '' not in parts and '.' not in parts


def _journal_log(data, directory):
    """The log of the last journal event that introduces or updates a log reference, or None.

    The chain was verified by the caller. References are relative to the tasks directory;
    one that is not a plain relative path is skipped, never opened.
    """
    previous, found, unsafe = set(), None, 0
    for line in data.splitlines():
        event = json.loads(line)
        current = set(_log_refs(event.get('state')))
        introduced = [pair for pair in _log_refs(event.get('state')) if pair not in previous]
        safe = [ref for _, ref in introduced if _safe_ref(ref)]
        unsafe += len(introduced) - len(safe)
        if safe:
            found = dict(ref=f'{directory}/{safe[-1]}', source='task journal', sequence=event.get('sequence'),
                         event=event.get('event') if isinstance(event.get('event'), str) else None,
                         recorded_at=None)
        previous = current
    return found, unsafe


def _recorded_at(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _last_log(journal_log, state, prefix):
    """Which persisted log is the last one, and why; its tail is read after the check.

    The task journal's order comes first: the last event that introduces or updates
    a log reference. Without one, the log registered as an artifact with the latest
    ``recorded_at``, the path breaking ties. Never a file's mtime.
    """
    found, unsafe = journal_log
    notes = [LOG_GAP]
    if unsafe:
        notes.append(f'{unsafe} task journal log references are not plain relative paths and were skipped')
    if found is None:
        artifacts = (state or {}).get('artifacts')
        candidates, unordered = [], 0
        for meta in (artifacts.values() if isinstance(artifacts, dict) else ()):
            path = meta.get('path') if isinstance(meta, dict) else None
            if not isinstance(path, str) or not path.endswith('.log'):
                continue
            moment = _recorded_at(meta.get('recorded_at'))
            if moment is None or not _safe_ref(path):
                unordered += 1
                continue
            candidates.append((moment, path, meta['recorded_at']))
        if unordered:
            notes.append(f'{unordered} registered logs have no usable recorded_at or path and were skipped')
        if candidates:
            _, path, recorded = max(candidates)
            found = dict(ref=prefix + path, source='artifacts', sequence=None, event=None, recorded_at=recorded)
    if found is None:
        return dict(ref=None, source=None, sequence=None, event=None, recorded_at=None, size_bytes=None,
                    tail_bytes=None, truncated=None, text=None, coverage=NONE,
                    reason='; '.join(['no log reference that can be ordered in the task journal or the artifacts']
                                     + notes[1:]))
    return dict(found, size_bytes=None, tail_bytes=None, truncated=None, text=None, coverage=PARTIAL,
                reason='; '.join(notes))


# CSI and OSC sequences, then any other two-character escape.
ESCAPES = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]?')
LINE_BREAKS = '\r\n\v\f\x1c\x1d\x1e\x1f\x85  '


def sanitize(text, *, newlines=False):
    """Text safe for a terminal: no escape sequences, no control or format characters.

    Each removed character becomes U+FFFD; tabs and, with ``newlines``, line feeds stay.
    Bidirectional overrides and other invisible format characters are removed too.
    """
    keep = '\t\n' if newlines else ''

    def safe(c):
        if c.isprintable() or c in keep:
            return c
        # A tab or a no-break space reads as a space; a line or page break never passes on one line.
        return ' ' if c.isspace() and c not in LINE_BREAKS else '�'
    return ''.join(safe(c) for c in ESCAPES.sub('�', str(text)))


def _read_tail(ticket, log):
    """The last ``TAIL_BYTES`` of the selected log, sanitized; never the whole file, never a symlink."""
    if log['ref'] is None:
        return log
    try:
        fd, info = _open_file(ticket, log['ref'])
    except Unverifiable as exc:
        return dict(log, reason=f'{log["reason"]}; the log is not readable: {exc}')
    if fd is None:
        return dict(log, reason=f'{log["reason"]}; the log no longer exists')
    try:
        start = max(0, info.st_size - TAIL_BYTES)
        data = os.pread(fd, TAIL_BYTES, start)
    except OSError as exc:
        return dict(log, reason=f'{log["reason"]}; the log is not readable: {type(exc).__name__}')
    finally:
        os.close(fd)
    return dict(log, size_bytes=info.st_size, tail_bytes=len(data), truncated=start > 0,
                text=sanitize(data.decode('utf-8', errors='replace'), newlines=True))


def _dag(plan, journal_state):
    if not isinstance(plan, dict) or not isinstance(plan.get('tasks'), list):
        raise Unverifiable('plan has no task list')
    records = (journal_state or {}).get('tasks') or {}
    if not isinstance(records, dict):
        raise Unverifiable('task journal state has no task map')
    for tid, rec in records.items():
        attempts = rec.get('attempts') if isinstance(rec, dict) else None
        if (not isinstance(rec, dict) or not isinstance(rec.get('status'), str)
                or not (attempts is None or (isinstance(attempts, int) and not isinstance(attempts, bool)))):
            raise Unverifiable(f'task journal record {tid!r} is malformed')
    status = {tid: rec['status'] for tid, rec in records.items()}
    nodes, counts = [], {}
    for task in plan['tasks']:
        if not isinstance(task, dict) or not isinstance(task.get('task_id'), str):
            raise Unverifiable('plan task without task_id')
        tid = task['task_id']
        record = records.get(tid)
        native = record['status'] if record else None
        depends = task.get('depends_on') or []
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            raise Unverifiable(f'plan task {tid!r} has malformed dependencies')
        depends = list(depends)
        waiting = [d for d in depends if status.get(d) != 'COMPLETED']
        nodes.append(dict(task_id=tid, title=task.get('title'), depends_on=depends, status=native,
                          recorded=record is not None, attempts=record.get('attempts') if record else None,
                          waiting_on=waiting,
                          ready=None if record is None else (native != 'COMPLETED' and not waiting)))
        key = native if native is not None else 'UNRECORDED'
        counts[key] = counts.get(key, 0) + 1
    planned = {n['task_id'] for n in nodes}
    return dict(plan_revision=plan.get('plan_revision'), nodes=nodes, counts=dict(sorted(counts.items())),
                outside_plan=sorted(t for t in records if t not in planned))


def _feedback(reader):
    for relative in FEEDBACK_FILES:
        reader.note(relative)
    if reader.seen['feedback/authority.json'] is None:
        if reader.seen['feedback/task-journal.jsonl'] is None:
            return dict(status='absent', projection=None, reason=None)
        return dict(status='unavailable', projection=None, reason='feedback history without authority')
    from ssdlc_feedback import FeedbackStore
    try:
        projection = FeedbackStore(reader.ticket).public_projection()
    except Exception as exc:  # The engine's refusal is a status here, never a crash.
        return dict(status='unavailable', projection=None, reason=f'{type(exc).__name__}: {exc}')
    return dict(status='active', projection=projection, reason=None)


def _amounts(values, nullable=True):
    """A map of dollar amounts as ``LimitsStore`` writes them: finite and non-negative.

    An observed ``usage`` may be None (unknown); a ``conservative_usage`` reserve never is,
    since ``LimitsStore`` adds it to the charge of its kind."""
    return isinstance(values, dict) and all(
        (v is None and nullable) or (type(v) in (int, float) and math.isfinite(v) and v >= 0)
        for v in values.values())


def _limits(reader, prefix):
    """The frozen limits of the generation, read without writing: ``{status, reason, policy, state}``.

    ``absent`` without a frozen profile; ``unavailable``, with the reason, when a limit
    extension activation is pending or the policy and its journal do not verify. The
    chain of limit extensions is not replayed: dispatch still does.
    """
    from ssdlc_recovery import validate_live_usage
    from ssdlc_tasks import TaskError, TaskJournal, digest, validate_document
    data, journal = reader.read(prefix + LIMITS_POLICY), reader.read(prefix + LIMITS_JOURNAL)
    pending = reader.read(prefix + LIMITS_PENDING)

    def result(status, reason=None, policy=None, state=None):
        return dict(status=status, reason=reason, policy=policy, state=state)
    if data is None and journal is None and pending is None:
        return result('absent', 'no frozen limits profile in this run')
    if pending is not None:
        return result('unavailable', 'a limit extension activation is pending')
    if data is None or journal is None:
        return result('unavailable', 'the frozen limits policy or its journal is missing')
    try:
        envelope = json.loads(data)
        validate_document('recovery-limits', envelope['policy'])
        validate_live_usage(envelope['policy'])
        # Only complete lines, as ``TaskJournal.peek``: an interrupted tail is not repaired.
        state = TaskJournal._replay(journal[:journal.rfind(b'\n') + 1])[0]
        if not isinstance(state, dict) or state.get('policy_sha256') != digest(envelope):
            return result('unavailable', 'the limits journal does not match the frozen policy')
        rows = state.get('invocations')
        if not isinstance(rows, dict) or not all(
                isinstance(row, dict) and _amounts(row.get('usage', {}))
                and _amounts(row.get('conservative_usage', {}), nullable=False) for row in rows.values()):
            return result('unavailable', 'the limits journal has malformed invocations')
    except (TaskError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return result('unavailable', f'frozen limits do not verify: {type(exc).__name__}')
    return result('active', None, envelope['policy'], state)


def metric(value, coverage, source, unit=None, reason=None):
    if coverage != COMPLETE and not reason:
        raise ValueError('a metric without complete coverage needs a reason')
    return dict(value=value, unit=unit, coverage=coverage, source=source, reason=reason)


def _metrics(state, dag, feedback):
    from ssdlc_telemetry import breakdown, classify, ledger_measures
    out = {}
    if dag is None:
        for name in ('tasks_total', 'tasks_completed', 'tasks_remaining', 'task_attempts'):
            out[name] = metric(None, NONE, 'plan and task journal', 'tasks' if name != 'task_attempts' else 'attempts',
                               'no task plan in this run')
    else:
        nodes = dag['nodes']
        recorded = all(n['recorded'] for n in nodes)
        gap = None if recorded else 'tasks without a journal record are not counted'
        completed = sum(n['status'] == 'COMPLETED' for n in nodes)
        out['tasks_total'] = metric(len(nodes), COMPLETE, 'plan', 'tasks')
        out['tasks_completed'] = metric(completed, COMPLETE, 'task journal', 'tasks')
        out['tasks_remaining'] = metric(len(nodes) - completed, COMPLETE, 'plan and task journal', 'tasks')
        out['task_attempts'] = metric(sum(n['attempts'] or 0 for n in nodes), COMPLETE if recorded else PARTIAL,
                                      'task journal', 'attempts', gap)
    ledger = state.get('ledger') if isinstance(state, dict) else None
    if not isinstance(ledger, list):
        for name, unit in (('invocations', 'invocations'), ('model_duration_sec', 'seconds'),
                           ('spend_usd', 'USD'), ('estimate_usd', 'USD'), ('unclassified_usd', 'USD'),
                           ('tokens_total', 'tokens')):
            out[name] = metric(None, NONE, 'state.json ledger', unit, 'no ledger in state.json')
    else:
        rows = [r for r in ledger if isinstance(r, dict)]
        kinds = [classify(r)[0] for r in rows]
        legacy = kinds.count('legacy')
        report = breakdown(rows)
        providers = report['providers'].values()
        invocations = sum(p['invocations']['completed'] + p['invocations']['incomplete'] for p in providers)
        out['invocations'] = metric(invocations, PARTIAL if legacy else COMPLETE, 'state.json ledger telemetry',
                                    'invocations', f'{legacy} legacy rows without telemetry are not counted' if legacy else None)
        durations = [r.get('duration_sec') for r in rows]
        timed = [d for d in durations if isinstance(d, (int, float)) and not isinstance(d, bool)]
        if len(timed) == len(rows):
            out['model_duration_sec'] = metric(sum(timed), COMPLETE, 'state.json ledger duration_sec', 'seconds')
        elif timed:
            out['model_duration_sec'] = metric(sum(timed), PARTIAL, 'state.json ledger duration_sec', 'seconds',
                                               f'{len(rows) - len(timed)} ledger rows without a duration')
        else:  # No persisted duration at all: there is nothing to add up, not zero seconds.
            out['model_duration_sec'] = metric(None, NONE, 'state.json ledger duration_sec', 'seconds',
                                               f'none of the {len(rows)} ledger rows has a duration')
        measures = ledger_measures(rows)
        unmeasured, unestimated = len(measures['unmeasured_spend']), len(measures['unmeasured_estimate'])
        out['spend_usd'] = metric(measures['spend_usd'], PARTIAL if unmeasured else COMPLETE,
                                  'state.json ledger telemetry', 'USD',
                                  f'{unmeasured} completed calls on a measured route reported no charge' if unmeasured else None)
        out['estimate_usd'] = metric(measures['estimate_usd'], PARTIAL if unestimated else COMPLETE,
                                     'state.json ledger telemetry', 'USD',
                                     f'{unestimated} completed calls reported no estimate' if unestimated else None)
        out['unclassified_usd'] = metric(measures['unclassified_usd'], COMPLETE, 'state.json ledger cost_usd', 'USD')
        unknown = sum(p['tokens']['total_unknown'] for p in providers)
        totals = [p['tokens']['total'] for p in providers if p['tokens']['total'] is not None]
        missing = unknown + legacy
        out['tokens_total'] = metric(
            sum(totals) if totals or not missing else None,
            COMPLETE if not missing else (PARTIAL if totals else NONE), 'state.json ledger telemetry', 'tokens',
            f'{missing} calls or legacy rows without a token total' if missing else None)
    if feedback['status'] == 'active':
        projection = feedback['projection']
        out['feedback_rounds'] = metric(sum(r['rounds'] for r in projection['requests']), COMPLETE,
                                        'feedback ledger', 'rounds')
        interventions = projection['operator_owner_amendments'] + projection['operator_repair_returns']
        out['interventions'] = metric(interventions, PARTIAL, 'feedback ledger', 'interventions',
                                      'only interventions explicitly recorded in the feedback ledger are counted')
    else:
        reason = 'no feedback ledger' if feedback['status'] == 'absent' else 'feedback ledger unavailable'
        out['feedback_rounds'] = metric(0 if feedback['status'] == 'absent' else None,
                                        COMPLETE if feedback['status'] == 'absent' else NONE,
                                        'feedback ledger', 'rounds', None if feedback['status'] == 'absent' else reason)
        out['interventions'] = metric(None, NONE, 'feedback ledger', 'interventions',
                                      f'{reason}: no intervention record to count')
    for name, reason in NOT_OBSERVABLE.items():
        out[name] = metric(None, NONE, None, 'seconds', reason)
    return out


def _collect(reader):
    generation, root, history = _generation(reader)
    prefix = '' if root == '.' else root + '/'
    present, state = reader.document(prefix + 'state.json')
    if present and not isinstance(state, dict):
        raise Unverifiable('state.json is not an object')
    if generation['kind'] == 'execution':
        # The same check as ``ssdlc_reconciliation._selected_execution_root``: the
        # historical rows themselves, not only their number, are still in place.
        ledger = (state or {}).get('ledger')
        if not isinstance(ledger, list) or ledger[:len(history)] != history:
            raise Unverifiable('executed state lost its historical ledger prefix')
    journal, journal_state, journal_log = _journal(reader, root)
    plan_present, plan = reader.document(prefix + PLAN)
    dag = _dag(plan, journal_state) if plan_present else None
    if dag is None and journal_state is not None:
        raise Unverifiable('task journal without its plan')
    feedback = _feedback(reader)
    run = None
    if state is not None:
        run = {key: state.get(key) for key in ('status', 'current_phase', 'current_step')}
    metrics = _metrics(state, dag, feedback)
    from ssdlc_workflow_projection import project
    from ssdlc_workflow_liveness import fencing
    ledger = (state or {}).get('ledger')
    limits = _limits(reader, prefix)
    projection = project(run, ledger if isinstance(ledger, list) else [], dag, limits, metrics)
    # What the liveness classification needs beyond the public fields; never published as is.
    native = None if state is None else {key: state.get(key) for key in ('status', 'failure', 'phase_boundary')}
    internal = dict(native=native, limits=limits, fencing=fencing(state))
    return dict(run=run, generation=generation, journal=journal, tasks=dag, feedback=feedback,
                metrics=metrics, projection=projection, log=_last_log(journal_log, state, prefix),
                _internal=internal)


def _empty(ticket, status, attempts, now, reason, sources=None):
    return dict(schema=SCHEMA, schema_version=SCHEMA_VERSION, ticket=ticket,
                read=dict(status=status, attempts=attempts, max_attempts=MAX_ATTEMPTS, observed_at=now, reason=reason),
                sources=sources or {}, run=None, generation=None, journal=None, tasks=None, feedback=None, metrics=None,
                projection=None, status=None, log=None)


def snapshot(repo, ticket, *, clock=time.time, fault=lambda point, attempt: None, kernel=None):
    """The ``WorkflowSnapshot`` of ``<repo>/.ssdlc/<ticket>``; it never writes.

    ``read.status`` is ``COHERENT``, ``TORN`` (the files kept changing for
    ``MAX_ATTEMPTS`` collections) or ``UNVERIFIABLE`` (missing ticket, or data
    that is stable on disk but does not read or verify). Only a coherent snapshot
    carries data. ``fault(point, attempt)`` is called after each collection,
    before the check (point ``collected``): tests use it to write concurrently.

    ``status`` (#347) adds liveness from ``kernel`` (``ssdlc_workflow_liveness.Kernel``
    by default): its writers are observed before each collection and again after it,
    then coverage, and only then are the files checked. A writer seen by either
    observation makes the run ``RUNNING``.
    """
    import ssdlc_workflow_liveness as liveness
    kernel = kernel or liveness.Kernel()
    now = datetime.fromtimestamp(clock(), timezone.utc).isoformat()
    if not isinstance(ticket, str) or not TICKET.fullmatch(ticket):
        return _empty(ticket, UNVERIFIABLE, 0, now, 'invalid ticket name')
    root = Path(repo).absolute() / '.ssdlc' / ticket
    if root.is_symlink() or not root.is_dir():
        return _empty(ticket, UNVERIFIABLE, 0, now, 'ticket not found')
    reason = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reader = Reader(root)
        before = kernel.writers(root)
        try:
            data = _collect(reader)
        except Changed as exc:
            reason = f'{exc} changed while it was read'
            continue
        except Unverifiable as exc:
            if reader.stable():
                return _empty(ticket, UNVERIFIABLE, attempt, now, str(exc), reader.sources())
            reason = 'files changed while they were read'
            continue
        fault('collected', attempt)
        internal = data.pop('_internal')
        observations = [before, kernel.writers(root)]
        components = kernel.coverage(root, data['generation']['root'], internal['fencing'])
        # Checked after the last observation: the files did not move while the kernel was read.
        if reader.stable():
            result = _empty(ticket, COHERENT, attempt, now, None, reader.sources())
            result.update(data)
            # The log is chosen from the coherent data; only its tail is read, after the check.
            result['log'] = _read_tail(root, data['log'])
            result['status'] = liveness.classify(ticket, repo, internal['native'], data['tasks'], internal['limits'],
                                                 observations, components)
            return result
        reason = 'files changed while they were read'
    return _empty(ticket, TORN, MAX_ATTEMPTS, now, reason)
