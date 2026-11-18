"""Automatic fencing proof for native transitions (#45, part c2). Protocol: docs/writer-fencing.md.

The proof is a versioned object, separate from the operator's authorization. It
replaces only the operator's attestation that every writer is stopped
(``source_fenced: true``), and only for a run admitted to automatic fencing in part
c1 (``ssdlc_role_fencing.admitted``). Every other exact authorization (controller,
proposal, plan, profile, budget, scope) stays required.

A transition asks for it by omitting ``source_fenced`` from the operator's
authorization. The host then, in ``TransitionStore.publish``:

1. refuses unless the source run is admitted (``RUN_NOT_ADMITTED``);
2. takes ``LOCK_EX | LOCK_NB`` on the canonical ticket's writer gate
   (``ssdlc_writer_gate.transition``), which refuses while a writer holds ``LOCK_SH``;
3. under the lock, reads the selected generation and refuses unless it is the source
   being transitioned, and checks with Docker that no container of the ticket is
   created, running, paused or restarting;
4. records the proof beside the authorization in the generation's ``intent.json`` and
   keeps the lock until the selector is published.

Historical intents have no proof: they stay manual attestations and are never
reclassified. Delegated contracts that carry their own proof keep it; for an
admitted run they also need this one.
"""
import contextlib
import json
import math
from pathlib import Path
import subprocess
import time

import ssdlc_role_fencing as role_fencing
import ssdlc_writer_gate as wg
from ssdlc_transitions import TransitionError, _regular
from ssdlc_writer_gate import GateError


KIND = 'ssdlc-automatic-fencing-proof'
SCHEMA_VERSION = 1
INTENT_KEY = 'fencing'
NOT_ADMITTED = 'RUN_NOT_ADMITTED'
LOCK_OPERATION = 'LOCK_EX|LOCK_NB'
DOCKER_STATUSES = ('created', 'restarting', 'running', 'paused')
# Contracts whose authorization is the host's delegated proof, not an operator's file.
DELEGATED = frozenset({'feedback-scope-delegation-v1', 'feedback-in-scope-return-v1'})


class FencingRefused(TransitionError):
    """Automatic fencing refused with a stable code; the manual path stays available."""

    def __init__(self, code, detail=''):
        self.code, self.detail = code, detail
        reason = f'{code}: {detail}' if detail else str(code)
        super().__init__(f'Automatic fencing refused ({reason}); '
                         'the manual path with the operator attestation source_fenced stays available')


def requested(authorization, proposal):
    """Whether an operator authorization asks for automatic fencing: it omits ``source_fenced``."""
    return (isinstance(proposal, dict) and proposal.get('authorization_contract') not in DELEGATED
            and isinstance(authorization, dict) and 'source_fenced' not in authorization)


def delegated(proposal):
    return isinstance(proposal, dict) and proposal.get('authorization_contract') in DELEGATED


def admitted(source_root):
    """Whether the source run was admitted in part c1 and never refused since."""
    try:
        state = json.loads(_regular(Path(source_root), 'state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, TransitionError):
        return False
    return role_fencing.admitted(state)


def selected_generation(ticket_root):
    """The generation the published selectors name, relative to the canonical ticket.

    Same precedence as ``ssdlc_transitions.runtime_root``, without its pending fences:
    the preparation being activated is pending by definition. A selector that does not
    validate is a refusal, never a default.
    """
    from ssdlc_transitions import TransitionStore
    ticket_root = Path(ticket_root)
    try:
        for store, runtimes in (('execution', 'execution-runtimes'), ('continuations', 'runtime-generations')):
            if (ticket_root / store / 'active.json').exists():
                selector = TransitionStore(ticket_root / store)
                pointer = selector._read(selector.root, 'active.json')
                projection = selector.load_pointer(pointer)
                if projection.get('transition_id', pointer['transition_id']) != pointer['transition_id']:
                    raise TransitionError('Selected projection names another generation')
                return f'{runtimes}/{pointer["transition_id"]}'
    except Exception as exc:
        raise GateError('generation_invalid', f'{type(exc).__name__}: {exc}') from None
    return '.'


def _within(path, root):
    return path == root or root in path.parents


def ticket_containers(ticket_root, *, run=subprocess.run):
    """Docker check: ``(reason, observed)``; ``reason`` is None only when no container may write.

    A container counts when it is created, restarting, running or paused and mounts the
    canonical ticket or a path inside it (role containers of an admitted run mount it
    read-only at its host path), or mounts a directory containing it read-write. Nothing
    is ever signalled. A Docker failure or an unreadable answer is a reason, never a proof.
    """
    root = Path(ticket_root).resolve()
    argv = ['docker', 'ps', '-a', '-q', '--no-trunc']
    for status in DOCKER_STATUSES:
        argv += ['--filter', f'status={status}']
    try:
        identities = run(argv, capture_output=True, text=True, timeout=15, check=True).stdout.split()
        containers = []
        if identities:
            data = run(['docker', 'inspect', *identities], capture_output=True, text=True, timeout=30, check=True)
            containers = json.loads(data.stdout)
            if not isinstance(containers, list) or len(containers) != len(identities):
                raise ValueError('unexpected docker inspect output')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f'docker could not list the containers of the ticket: {type(exc).__name__}', None
    found = 0
    for container in containers:
        try:
            mounts = container.get('Mounts') or []
            hit = False
            for mount in mounts:
                for key in ('Source', 'Destination'):
                    value = mount.get(key)
                    if value and _within(Path(value).resolve(), root):
                        hit = True
                source = mount.get('Source')
                if source and mount.get('RW') is not False and _within(root, Path(source).resolve()):
                    hit = True
        except (AttributeError, TypeError, ValueError, OSError):
            hit = True  # an answer we cannot read never proves anything
        found += hit
    observed = dict(statuses=list(DOCKER_STATUSES), listed=len(identities), ticket_containers=found)
    if found:
        return f'{found} container(s) of the ticket may still run', observed
    return None, observed


def _identity(value):
    return (isinstance(value, dict) and set(value) == {'pid', 'boot_id', 'start_ticks'}
            and wg.is_identity(value))


def validate_proof(proof):
    """Structural check of a recorded proof. It never re-proves anything after the fact."""
    def integer(value, minimum=0):
        return type(value) is int and value >= minimum
    ok = (isinstance(proof, dict)
          and set(proof) == {'kind', 'schema_version', 'gate', 'generation', 'lock', 'docker', 'verified_at'}
          and proof['kind'] == KIND and type(proof['schema_version']) is int
          and proof['schema_version'] == SCHEMA_VERSION
          and isinstance(proof['gate'], dict) and set(proof['gate']) == {'device', 'inode'}
          and integer(proof['gate']['device']) and integer(proof['gate']['inode'], 1)
          and isinstance(proof['generation'], str) and proof['generation']
          and isinstance(proof['lock'], dict) and set(proof['lock']) == {'operation', 'acquired', 'receipt', 'holder'}
          and proof['lock']['operation'] == LOCK_OPERATION and proof['lock']['acquired'] is True
          and proof['lock']['receipt'] == 'writer-gate-exclusive-v1' and _identity(proof['lock']['holder'])
          and isinstance(proof['docker'], dict)
          and set(proof['docker']) == {'statuses', 'listed', 'ticket_containers'}
          and proof['docker']['statuses'] == list(DOCKER_STATUSES) and integer(proof['docker']['listed'])
          and proof['docker']['ticket_containers'] == 0 and type(proof['docker']['ticket_containers']) is int
          and type(proof['verified_at']) in (int, float) and math.isfinite(proof['verified_at'])
          and proof['verified_at'] > 0)
    if not ok:
        raise TransitionError(f'Invalid automatic fencing proof ({KIND} v{SCHEMA_VERSION} required)')
    return proof


def same_fence(stored, fresh):
    """A replay's fresh proof covers the stored one: same gate and same source generation."""
    return stored['gate'] == fresh['gate'] and stored['generation'] == fresh['generation']


def evidence(intent):
    """How an intent's source was fenced, keeping operator and host evidence apart.

    ``operator_attested`` is the operator's own ``source_fenced: true``; ``automatic``
    is the host's proof or None. A historical intent has no proof, so it never reads
    as an automatic one.
    """
    intent = intent if isinstance(intent, dict) else {}
    authorization, proof = intent.get('authorization'), intent.get(INTENT_KEY)
    attested = isinstance(authorization, dict) and authorization.get('source_fenced') is True
    if delegated(intent.get('proposal')):
        mode = 'delegated'
    else:
        mode = 'manual' if attested else 'automatic' if proof is not None else 'unfenced'
    return dict(mode=mode, operator_attested=attested, automatic=proof)


class Fence:
    """A held ``LOCK_EX`` and its recorded proof. Not serializable, not a capability."""

    def __init__(self, proof, gate_proof):
        self.proof, self._gate_proof = proof, gate_proof

    def check(self):
        """The exclusive lock is still ours: called right before the selector is published."""
        try:
            self._gate_proof.check()
        except GateError as exc:
            raise FencingRefused(exc.code, exc.detail) from None


@contextlib.contextmanager
def fence(ticket_root, source_root, *, docker=subprocess.run, clock=time.time):
    """Prove that no writer of ``source_root`` runs, and hold ``LOCK_EX`` while publishing.

    Refuses with ``FencingRefused`` (never a wait) when the run is not admitted, the
    environment is unsupported, a writer holds the gate, the source is not the selected
    generation, a host command ran in it, or a container of the ticket may still run.
    """
    ticket_root, source_root = Path(ticket_root).absolute(), Path(source_root).absolute()
    if not admitted(source_root):
        raise FencingRefused(NOT_ADMITTED, 'the source run was not created with writer_fencing v1, '
                                           'or automatic fencing was refused for it since')
    try:
        expected = source_root.relative_to(ticket_root).as_posix()
    except ValueError:
        raise FencingRefused('generation_invalid', 'the source is outside the canonical ticket') from None
    observed = {}

    def probe():
        reason, result = ticket_containers(ticket_root, run=docker)
        observed.update(result or {})
        return reason

    try:
        with wg.transition(ticket_root, lambda: selected_generation(ticket_root), coverage=[probe]) as gate_proof:
            if gate_proof.generation != expected:
                raise GateError('stale_generation', f'the selected generation is {gate_proof.generation}, '
                                                    f'not the source {expected}')
            if not admitted(source_root):  # a refusal recorded before the lock was taken
                raise GateError(NOT_ADMITTED, 'automatic fencing was refused for the source run')
            receipt = gate_proof.check()
            proof = validate_proof(dict(
                kind=KIND, schema_version=SCHEMA_VERSION,
                gate=dict(device=receipt['device'], inode=receipt['inode']),
                generation=expected,
                lock=dict(operation=LOCK_OPERATION, acquired=True, receipt=receipt['kind'],
                          holder=receipt['holder']),
                docker=dict(observed), verified_at=clock()))
            yield Fence(proof, gate_proof)
    except GateError as exc:
        raise FencingRefused(exc.code, exc.detail) from None
