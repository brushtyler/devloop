"""``workflow-status``: the ``WorkflowSnapshot`` of a run as a command (#46, part 4, #349).

The text output has four sections: run, generation and liveness; tasks and feedback;
metrics, budget and projections; next action, prerequisites and the last persisted
log. ``--json`` prints the snapshot contract unchanged (``schema_version`` 1), as one
document; with ``--watch`` each snapshot is one JSON line (JSON Lines). ``--watch``
takes a snapshot every ``--interval`` seconds (default 2, minimum 1) and keeps
sampling after terminal or error snapshots, until SIGINT, which ends it with 130
and no traceback.

Every string of the text output is sanitized (no escape sequences, no control or
format characters); JSON escapes them. Like the snapshot, the command never writes
under ``.ssdlc/``.

Exit codes, in this order of precedence: 2 invalid arguments; 5 ``TORN``; 6 a read
that is ``UNVERIFIABLE`` or fails; 4 ``STALE``; 3 a stopped run or a fenced dispatch;
0 a completed run; 6 ``UNVERIFIED`` liveness or an absent or unknown outcome; 0
otherwise. Partial metrics alone never change it. In ``--watch`` the code of each
snapshot is not an exit: only SIGINT ends it.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import shlex
import sys
import time

import ssdlc_workflow_status as ws

DEFAULT_INTERVAL, MIN_INTERVAL = 2.0, 1.0
OK, INVALID_ARGUMENTS, STOPPED, STALE, TORN, UNVERIFIABLE, INTERRUPTED = 0, 2, 3, 4, 5, 6, 130
SECTIONS = ('Run, generation and liveness', 'Tasks and feedback', 'Metrics, budget and projections',
            'Next action, prerequisites and last log')
CLEAR = '\x1b[H\x1b[2J'


def exit_code(snapshot):
    """The exit code of one snapshot (2, invalid arguments, is decided before any read)."""
    read = (snapshot or {}).get('read') or {}
    if read.get('status') == ws.TORN:
        return TORN
    if read.get('status') != ws.COHERENT:
        return UNVERIFIABLE
    status = snapshot.get('status') or {}
    liveness, outcome = status.get('liveness'), status.get('outcome')
    if liveness == 'STALE':
        return STALE
    if outcome == 'stopped' or (snapshot.get('generation') or {}).get('dispatch_fenced'):
        return STOPPED
    if outcome == 'completed':
        return OK
    if liveness == 'UNVERIFIED' or outcome in (None, 'absent', 'unknown'):
        return UNVERIFIABLE
    return OK


def take(repo, ticket, *, snapshot=ws.snapshot, clock=time.time):
    """A snapshot; a read that fails is an ``UNVERIFIABLE`` snapshot with its reason, never a traceback."""
    try:
        return snapshot(repo, ticket)
    except Exception as exc:  # noqa: BLE001 - a failed read is exit 6, not a crash.
        now = datetime.fromtimestamp(clock(), timezone.utc).isoformat()
        return ws._empty(ticket, ws.UNVERIFIABLE, 0, now, f'read error: {type(exc).__name__}: {exc}')


# ---------------------------------------------------------------------------
# Text rendering. Every value from the run goes through ``_s``.

def _s(value):
    if value is None:
        return '-'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return f'{value:.6f}'.rstrip('0').rstrip('.') if math.isfinite(value) else ws.sanitize(value)
    return ws.sanitize(value)


def _metric(item):
    text = _s(item.get('value'))
    if item.get('value') is not None and item.get('unit'):
        text += ' ' + _s(item['unit'])
    if item.get('coverage') != ws.COMPLETE:
        text += f" [{_s(item.get('coverage'))}: {_s(item.get('reason'))}]"
    return text


def _run_section(snap):
    run, generation, status = snap['run'] or {}, snap['generation'] or {}, snap['status'] or {}
    lines = [f"State       : {_s(status.get('state'))} ({_s(status.get('outcome'))}), "
             f"phase {_s(run.get('current_phase'))}, step {_s(run.get('current_step'))}",
             f"Generation  : {_s(generation.get('kind'))}, transition {_s(generation.get('transition_id'))}, "
             f"dispatch fenced: {_s(bool(generation.get('dispatch_fenced')))}"]
    writers = ', '.join(f"{_s(w.get('lock'))} {_s(w.get('mode'))} pid {_s(w.get('pid'))}"
                        for w in status.get('writers') or ()) or 'none'
    lines.append(f"Liveness    : {_s(status.get('liveness'))}; writers: {writers}")
    coverage = status.get('coverage') or {}
    lines.append(f"Coverage    : {'complete' if coverage.get('complete') else 'incomplete'}")
    for name, part in (coverage.get('components') or {}).items():
        if not part.get('covered'):
            lines.append(f"  - {_s(name)}: {_s(part.get('reason'))}")
    activity = status.get('activity') or {}
    lines.append(f"Activity    : {_s(activity.get('value'))} ({len(activity.get('invocations') or ())} invocations, "
                 f"{len(activity.get('verifications') or ())} verifications recorded in progress)")
    if activity.get('reason'):
        lines.append(f"  {_s(activity['reason'])}")
    return lines


def _tasks_section(snap):
    tasks, journal, feedback = snap['tasks'], snap['journal'], snap['feedback'] or {}
    lines = []
    if tasks is None:
        lines.append('Tasks       : no task plan in this run')
    else:
        counts = ', '.join(f'{_s(k)} {_s(v)}' for k, v in tasks['counts'].items()) or 'none'
        lines.append(f"Tasks       : plan revision {_s(tasks.get('plan_revision'))}, "
                     f"{len(tasks['nodes'])} tasks: {counts}")
        for node in tasks['nodes']:
            state = 'ready' if node.get('ready') else (
                'waiting on ' + ', '.join(_s(d) for d in node['waiting_on']) if node.get('waiting_on') else '')
            lines.append(f"  {_s(node['task_id'])}  {_s(node.get('status') or 'UNRECORDED')}  "
                         f"attempts {_s(node.get('attempts'))}  {state}".rstrip())
        if tasks.get('outside_plan'):
            lines.append('  Outside the plan: ' + ', '.join(_s(t) for t in tasks['outside_plan']))
    if journal is not None:
        lines.append(f"Journal     : sequence {_s(journal.get('sequence'))}, "
                     f"interrupted tail {_s(journal.get('interrupted_tail_bytes'))} bytes")
    text = f"Feedback    : {_s(feedback.get('status'))}"
    if feedback.get('reason'):
        text += f" ({_s(feedback['reason'])})"
    lines.append(text)
    metrics = snap['metrics'] or {}
    for name in ('feedback_rounds', 'interventions'):
        if name in metrics:
            lines.append(f"  {name}: {_metric(metrics[name])}")
    return lines


def _projection_lines(projection):
    lines = [f"Projection  : coverage {_s(projection.get('coverage'))}, tasks remaining "
             f"{_s(projection.get('tasks_remaining'))}, gates remaining "
             + (', '.join(_s(g) for g in projection.get('gates_remaining') or ()) or 'none'
                if projection.get('gates_remaining') is not None else '-')]
    limits = projection.get('limits') or {}
    text = f"  Limits: {_s(limits.get('status'))}"
    if limits.get('reason'):
        text += f" ({_s(limits['reason'])})"
    lines.append(text)
    for measure, entry in (projection.get('measures') or {}).items():
        lines.append(f"  {_s(measure)}: spent {_metric(entry['spent'])}; central {_metric(entry['central'])}")
        budget = entry.get('budget')
        if budget:
            lines.append(f"    budget {_s(entry.get('cap_kind'))}: cap {_s(budget.get('cap'))}, accounted "
                         f"{_s(budget.get('accounted'))}, remaining {_s(budget.get('remaining'))}, projected "
                         f"{_s(budget.get('projected'))}, over cap {_s(budget.get('over_cap'))}")
        scenario = entry.get('scenario')
        if scenario:
            lines.append(f"    scenario (not a guaranteed maximum): {_metric(scenario)}")
    for warning in projection.get('warnings') or ():
        lines.append(f"  Warning {_s(warning.get('code'))} on {_s(warning.get('measure'))} (no effect on the run)")
    return lines


def _metrics_section(snap):
    lines = []
    for name, item in (snap['metrics'] or {}).items():
        if name not in ('feedback_rounds', 'interventions'):
            lines.append(f"{name}: {_metric(item)}")
    if snap['projection']:
        lines += _projection_lines(snap['projection'])
    return lines


def _action_section(snap):
    status, log = snap['status'] or {}, snap['log'] or {}
    lines = []
    stop = status.get('stop')
    if stop:
        lines.append(f"Stop        : {_s(stop.get('class'))} ({_s(stop.get('reason_code'))}), mode "
                     f"{_s(stop.get('mode'))}, category {_s(stop.get('category'))}")
        if stop.get('detail'):
            lines.append(f"  {_s(stop['detail'])}")
    action = status.get('next_action') or {}
    lines.append(f"Next action : {_s(action.get('action'))}")
    lines.append(f"Mode        : {_s(action.get('mode'))}")
    command = action.get('command')
    lines.append('Command     : ' + (f"{_s(shlex.join(command['argv']))} (in {_s(command.get('cwd'))})"
                                     if command else 'none: the action is manual or misses an argument'))
    lines.append('Prerequisites:' if action.get('prerequisites') else 'Prerequisites: none')
    lines += [f"  - {_s(item)}" for item in action.get('prerequisites') or ()]
    if log.get('ref') is None:
        lines.append(f"Last log    : none ({_s(log.get('reason'))})")
        return lines
    where = (f"journal event {_s(log.get('sequence'))} {_s(log.get('event'))}" if log.get('source') == 'task journal'
             else f"artifact recorded at {_s(log.get('recorded_at'))}")
    lines.append(f"Last log    : {_s(log['ref'])} ({_s(log.get('source'))}, {where})")
    lines.append(f"  coverage {_s(log.get('coverage'))}: {_s(log.get('reason'))}")
    if log.get('text') is not None:
        lines.append(f"  last {_s(log.get('tail_bytes'))} of {_s(log.get('size_bytes'))} bytes"
                     + (' (truncated)' if log.get('truncated') else '') + ':')
        lines += ['  | ' + line for line in log['text'].split('\n')]
    return lines


def render(snap, code):
    """The four text sections of a snapshot; every value from the run is sanitized."""
    read = snap.get('read') or {}
    head = (f"workflow-status {_s(snap.get('ticket'))}: read {_s(read.get('status'))} "
            f"(attempt {_s(read.get('attempts'))}/{_s(read.get('max_attempts'))}) at "
            f"{_s(read.get('observed_at'))}, exit {code}")
    lines = [head]
    if read.get('reason'):
        lines.append(f"  {_s(read['reason'])}")
    bodies = ((_run_section, _tasks_section, _metrics_section, _action_section)
              if read.get('status') == ws.COHERENT else ())
    for index, title in enumerate(SECTIONS):
        lines += ['', f'== {index + 1}. {title} ==']
        body = bodies[index](snap) if bodies else [f"no data: the snapshot is {_s(read.get('status'))}"]
        lines += ['  ' + line for line in body]
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Command.

def _interval(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'not a number: {text!r}') from None
    if not math.isfinite(value) or value < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(f'the interval is at least {MIN_INTERVAL:g} second')
    return value


def _ticket(text):
    if not ws.TICKET.fullmatch(text):
        raise argparse.ArgumentTypeError(f'invalid ticket name {text!r}')
    return text


def parser():
    p = argparse.ArgumentParser(prog='workflow-status', description=(
        'Read-only status of a run in four sections; never writes under .ssdlc/. Exit codes: 0 operational or '
        'completed, 2 invalid arguments, 3 stopped or fenced, 4 STALE, 5 TORN, 6 not verifiable or read '
        'error, 130 SIGINT.'))
    p.add_argument('--ticket', '-t', required=True, type=_ticket)
    p.add_argument('--repo', '-r', default='.')
    p.add_argument('--json', action='store_true', help='the snapshot as JSON; JSON Lines with --watch')
    p.add_argument('--watch', action='store_true', help='refresh until SIGINT (Ctrl-C), which exits with 130')
    p.add_argument('--interval', type=_interval, default=None,
                   help=f'seconds between snapshots with --watch (default {DEFAULT_INTERVAL:g}, '
                        f'minimum {MIN_INTERVAL:g})')
    return p


def _emit(out, snap, code, *, as_json, watch):
    if as_json:
        text = (json.dumps(snap, sort_keys=True, separators=(',', ':')) if watch
                else json.dumps(snap, sort_keys=True, indent=2)) + '\n'
    else:
        text = (CLEAR if watch and out.isatty() else '') + render(snap, code)
    out.write(text)  # one write per snapshot: a JSON line is never split by the program
    out.flush()


def main(argv=None, *, out=None, snapshot=ws.snapshot, sleep=time.sleep):
    out = out or sys.stdout
    p = parser()
    try:
        args = p.parse_args(argv)
        if args.interval is not None and not args.watch:
            p.error('--interval needs --watch')
    except SystemExit as exc:  # argparse: 2 for invalid arguments, 0 for --help
        return exc.code if isinstance(exc.code, int) else INVALID_ARGUMENTS
    interval = args.interval or DEFAULT_INTERVAL
    try:
        while True:
            snap = take(args.repo, args.ticket, snapshot=snapshot)
            code = exit_code(snap)
            _emit(out, snap, code, as_json=args.json, watch=args.watch)
            if not args.watch:
                return code
            # Terminal or error snapshots do not end the watch: only SIGINT does.
            sleep(interval)
    except KeyboardInterrupt:
        if not args.json:
            try:
                out.write('\n')
                out.flush()
            except (OSError, ValueError, KeyboardInterrupt):
                pass
        return INTERRUPTED


if __name__ == '__main__':
    sys.exit(main())
