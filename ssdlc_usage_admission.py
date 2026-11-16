"""Operator-only admission of a live unknown-usage model to a stopped I2 generation.

Issue #43: a live call without a reported dollar cost is never counted as zero.
An already active profile gains ``live_unknown_usage`` only through this exact,
hash-bound transition. It keeps counters, caps and the deadline unchanged. The
live rows already recorded without cost and without a model are listed exactly
in the authorization: they stop blocking, stay visible as "not covered" and are
never reclassified or priced.
"""
import copy
from pathlib import Path

from ssdlc_recovery import uncovered, validate_live_usage
from ssdlc_tasks import TaskError, digest, validate_document


CONTRACT = 'live-usage-profile-admission-v1'


def legacy_uncovered(counters):
    return sorted(identity for identity, row in counters['invocations'].items() if uncovered(row))


def subject(source, plan, profile, reason):
    validate_document('recovery-limits', profile)
    try:
        validate_live_usage(profile)
    except (ValueError, KeyError, AttributeError) as exc:
        raise TaskError('Live unknown-usage model is invalid: ' + str(exc)) from None
    if plan != source['plan'] or not isinstance(reason, str) or not reason.strip() or len(reason) > 1600:
        raise TaskError('Live-usage admission cannot amend a plan or omit its reason')
    old = source['envelope']['policy']
    declared, previous = profile.get('live_unknown_usage', {}), old.get('live_unknown_usage', {})
    if not declared or any(declared.get(engine) != rule for engine, rule in previous.items()) or declared == previous:
        raise TaskError('Live-usage admission must add an engine model without changing declared ones')
    normalized = copy.deepcopy(profile)
    normalized.pop('live_unknown_usage')
    if previous:
        normalized['live_unknown_usage'] = copy.deepcopy(previous)
    if normalized != old:
        raise TaskError('Live-usage admission cannot change counters, caps, deadline or other profile fields')
    value = dict(schema_version=1, compatibility=CONTRACT, parent_selector=source['parent_selector'],
                 source_files_sha256=digest(source['files']), old_plan_sha256=digest(plan), new_plan_sha256=digest(plan),
                 scope_diff={}, task_id=source['task_id'], reason=reason,
                 counters_sha256=digest(source['counters']), journal_sha256=source['files']['03-dev/tasks/task-journal.jsonl'],
                 profile_sha256=digest(profile), old_profile_sha256=digest(old),
                 live_unknown_usage=copy.deepcopy(declared),
                 legacy_uncovered_invocations=legacy_uncovered(source['counters']))
    binding = feedback_binding(source)
    if binding is not None:
        value['feedback_binding'] = binding
    return value


def admitted_engines(old, profile):
    """Engines whose live unknown-usage model this admission adds."""
    previous = old.get('live_unknown_usage', {})
    return sorted(engine for engine in profile.get('live_unknown_usage', {}) if engine not in previous)


def feedback_binding(source):
    """Bind an active feedback ledger, as the other native admissions do.

    The source must be the generation the recording authority was frozen on,
    or one it already admitted. An unknown role outcome is never carried.
    """
    from ssdlc_feedback import FeedbackStore
    store = FeedbackStore(source['root'].parent.parent)
    if not store.root.exists():
        return None
    from ssdlc_controller import execution_contract
    from ssdlc_feedback_delegation import admitted
    authority, state = store._read()
    if state is None or authority['status'] != 'ACTIVE':
        raise TaskError('Live-usage admission requires a complete feedback authority')
    policy, envelope = authority['policy'], source['envelope']
    frozen = (policy['limits_sha256'] == digest(envelope)
              and policy['controller_sha256'] == digest(execution_contract(envelope['binding']['controller'])))
    if not frozen and not admitted(state, source):
        raise TaskError('Live-usage admission feedback source is outside admitted lineage')
    if any(call['status'] == 'INTENT' for call in state['role_calls']):
        raise TaskError('Unknown feedback outcome requires reconciliation before live-usage admission')
    return dict(authority_sha256=digest(authority), state_sha256=digest(state))


def _admission(proposal, recipe, projection):
    return dict(transition_id=proposal['transition_id'], limits_sha256=digest(recipe['envelope']),
                profile_sha256=digest(recipe['envelope']['policy']),
                controller_sha256=proposal['new_controller_sha256'],
                parent_selector=proposal['parent_selector'], projection_sha256=digest(projection))


CARRIED = ('carried_requests', 'carried_role_calls')


def _core(entry):
    return {k: v for k, v in entry.items() if k not in CARRIED}


def finalize(ticket, proposal, recipe, projection):
    """Admit the new generation to the feedback ledger after its selector publish.

    Open requests bound to the parent generation, still REQUESTED or PROPOSED
    without a review, are carried: their next identical submission rebinds
    them to this generation. A planner call already RETURNED is carried with
    its receipt unchanged: the entry lists its previous native binding and the
    call is rebound to this generation, which holds the same invocation row.
    The captured response is reused, never redispatched. Recording caps,
    consumed role calls and amendment quotas stay unchanged.
    """
    if proposal.get('authorization_contract') != CONTRACT or 'feedback_binding' not in recipe['subject']:
        return
    from ssdlc_feedback import FeedbackStore
    from ssdlc_tasks import ticket_lock
    store = FeedbackStore(ticket)
    expected = _admission(proposal, recipe, projection)
    with ticket_lock(store.root / 'feedback.lock'):
        authority, state = store._read()
        prior = state.get('admitted_generations', {}).get(proposal['transition_id'])
        if prior is not None:
            if _core(prior) != expected:
                raise TaskError('Live-usage admission reconciliation changed')
            return
        if recipe['subject']['feedback_binding'] != dict(authority_sha256=digest(authority), state_sha256=digest(state)):
            raise TaskError('Feedback changed before live-usage admission reconciliation')
        carried, calls, limits = [], {}, digest(recipe['envelope'])
        for identity, row in state['requests'].items():
            consulted = [call for call in state['role_calls'] if call['request_id'] == identity]
            # Only a captured planner return is carried: its proposal has no
            # review yet. An INTENT was refused at preparation.
            if (row['status'] in ('REQUESTED', 'PROPOSED')
                    and not row.get('source_reconciliation_required', True)
                    and row['binding']['parent_selector'] == proposal['parent_selector']
                    and (len(consulted) == 1 if row['status'] == 'PROPOSED' else len(consulted) <= 1)
                    and all(call['role'] == 'planner' and call['status'] == 'RETURNED' and 'native' in call
                            for call in consulted)):
                carried.append(identity)
                for call in consulted:
                    # Counters are carried unchanged: the same native invocation
                    # now belongs to the admitted generation. The receipt stays.
                    calls[call['operation_id']] = copy.deepcopy(call['native'])
                    call['native'] = dict(call['native'], limits_sha256=limits)
        expected['carried_requests'] = sorted(carried)
        expected['carried_role_calls'] = calls
        state.setdefault('admitted_generations', {})[proposal['transition_id']] = expected
        store._append('LIVE_USAGE_ADMISSION_APPLIED', state)


def ensure_reconciled(ticket, projection, proposal):
    """Dispatch stays fenced until the admitted generation is in the feedback ledger."""
    from ssdlc_transitions import _regular
    import json
    recipe = json.loads(_regular(Path(ticket) / 'execution' / proposal['transition_id'], 'recipe.json').read_text())
    if 'feedback_binding' not in recipe['subject']:
        return
    from ssdlc_feedback import FeedbackStore
    entry = FeedbackStore(ticket).inspect().get('admitted_generations', {}).get(proposal['transition_id'])
    if entry is None or _core(entry) != _admission(proposal, recipe, projection):
        raise TaskError('Live-usage admission feedback reconciliation pending; dispatch is fenced')


def prepare(ticket, repo, profile, reason, **kwargs):
    from ssdlc_scope_amendments import inspect, prepare as native_prepare
    from ssdlc_locks import transition_lock
    with transition_lock(Path(ticket) / 'ticket.lock', kwargs.get('held_locks', ())) as lease:
        source = inspect(ticket, allow_host_hints=True)
        held = kwargs.get('held_locks', ())
        held = held if any(value is lease for value in held) else (*held, lease)
        return native_prepare(ticket, repo, source['plan'], None, reason, profile_update=profile,
                              profile_contract=CONTRACT, **dict(kwargs, held_locks=held))


def activate(ticket, preparation, authorization, **kwargs):
    import json
    from ssdlc_transitions import _regular
    proposal = json.loads(_regular(Path(preparation), 'proposal.json').read_text())
    if proposal.get('authorization_contract') != CONTRACT:
        raise TaskError('Live-usage activation requires its own admission contract')
    from ssdlc_scope_amendments import activate as native_activate
    return native_activate(ticket, preparation, authorization, **kwargs)


def main(argv=None):
    import argparse
    import json
    import re
    import sys
    from ssdlc_controller import controller_snapshot, assert_controller, execution_contract
    from ssdlc_feedback_delegation import observe_environment
    from ssdlc_preparations import docker_quiescent
    parser = argparse.ArgumentParser(description='Explicit live unknown-usage profile admission; no model calls')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('prepare', 'activate'):
        p = commands.add_parser('usage-profile-' + name)
        p.add_argument('--repo', required=True); p.add_argument('--ticket', required=True)
        if name == 'prepare':
            p.add_argument('--profile', required=True); p.add_argument('--reason', required=True)
        else:
            p.add_argument('--preparation', required=True); p.add_argument('--authorization', required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.ticket) or args.ticket in {'.', '..'}:
        parser.error('Invalid ticket')
    repo = Path(args.repo).resolve(); ticket = repo / '.ssdlc' / args.ticket
    read = lambda path: json.loads(Path(path).read_text())
    try:
        controller = Path(__file__).resolve().parent; snapshot = controller_snapshot(controller)
        environment = observe_environment()
        identity = dict(controller_sha256=digest(execution_contract(snapshot)), environment=environment)
        def guard():
            assert_controller(controller, snapshot)
            if observe_environment() != environment or not docker_quiescent(dict(ticket_root=str(ticket),
                    runtime='execution-runtimes', preparation='execution', source_root=str(ticket))):
                raise TaskError('Live-usage admission environment or writer ownership changed')
        if args.command.endswith('prepare'):
            result = prepare(ticket, repo, read(args.profile), args.reason, runtime_identity=identity,
                             controller_snapshot=snapshot, guard=guard)
            subject_value = json.loads((Path(result['preparation']) / 'recipe.json').read_text())['subject']
            result = dict(preparation=result['preparation'], proposal_sha256=digest(result['proposal']),
                          transition_id=result['proposal']['transition_id'], execution_authorized=False,
                          legacy_uncovered_invocations=subject_value['legacy_uncovered_invocations'])
        else:
            preparation = Path(args.preparation).absolute()
            if preparation.parent != ticket / 'execution':
                raise TaskError('Activation requires the canonical preparation')
            if read(preparation / 'recipe.json')['runtime_identity'] != identity:
                raise TaskError('Prepared controller or environment differs from the executing runtime')
            result = activate(ticket, preparation, read(args.authorization), guard=guard)
            result = dict(transition_id=result['transition_id'], ready_for_signoff=False)
        print(json.dumps(dict(result, model_dispatches=0), sort_keys=True)); return 0
    except (TaskError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='USAGE_PROFILE_REFUSED', reason=str(exc)[:240], model_dispatches=0)), file=sys.stderr)
        return 2
