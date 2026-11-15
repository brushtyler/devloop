# Writer fencing protocol

Automatic fencing (#45) lets a transition prove, with a kernel lock, that no writer
of the canonical ticket is running. This page describes the protocol implemented in
`ssdlc_writer_gate.py`.

> **Status: part (a) of #45.** The gate, its protocol and its tests exist, but
> nothing uses them on real runs yet. Transitions, the supervisor and the delegated
> feedback flow behave exactly as before. Part (c) wires the gate into them.

## Invariants

1. **One stable gate per canonical ticket.** The gate is `.ssdlc/<ticket>/writer.gate`,
   in the canonical ticket directory, and is shared by every generation
   (`execution-runtimes/*`, `runtime-generations/*`). It is never deleted or recreated.
2. **Only the kernel lock is evidence.** A writer is admitted only while its
   descriptor holds `LOCK_SH`. A transition is admitted only while its descriptor holds
   `LOCK_EX`. Files, records and PIDs never stand in for the lock.
3. **Writers hold `LOCK_SH` for their whole life** and recheck the generation **after**
   acquiring it. A writer of the previous generation does not restart: it refuses with
   `stale_generation`.
4. **A transition takes `LOCK_EX | LOCK_NB`.** Success proves that no conforming writer
   is registered. Failure is a refusal (`writers_active`), never a wait. The exclusive
   lock is held only from source revalidation to selector publication, not for a whole
   session.
5. **The lock is not converted from shared to exclusive.** `flock` conversion releases
   the old lock before taking the new one, so it is not atomic. A process that holds a
   writer lease and needs a transition uses the explicit handoff below. No second lock
   file is used to close admission early: the non-blocking exclusive attempt needs none.
6. **Every refusal is diagnosable** through a `GateError` with a stable `code`. A refusal
   never yields a proof.

## The gate file

The gate is created once. A complete record (`ssdlc-writer-gate-v1`, holding the
gate's own inode number) is written to a temporary file and published with `link(2)`,
which never replaces an existing path. Concurrent creators converge on the same file.

Every opening verifies the gate's identity:

- the ticket directory and the gate are opened with `O_NOFOLLOW`, relative to the
  ticket directory descriptor;
- device and inode of the open descriptor must equal those of the path (`fstat` against
  `lstat`), both before and after the lock is acquired;
- the inode recorded in the gate must equal the open inode, so a copy of the gate is
  not the gate;
- a caller may bind the identity it saw earlier (`expected_identity`); a different
  identity is refused.

| Situation | Refusal code |
| --- | --- |
| Gate path is a symlink | `gate_symlink` |
| Gate path names another file (replaced, copied, recreated) | `gate_replaced` |
| Gate path is a directory, FIFO or device | `gate_not_regular` |
| Gate file is empty or holds a foreign record | `gate_corrupt` |
| Ticket directory is a symlink or a relative path | `ticket_root_invalid` |
| Filesystem without host-local kernel flock | `unsupported_filesystem` |

A refused gate is never repaired automatically: an operator decides what happened.

## Descriptor lifecycle

- Gate descriptors are opened `O_RDONLY | O_CLOEXEC` and are private to the process
  and thread that opened them. They are registered as `LockLease` receipts
  (`ssdlc_locks.record_lock`), which are thread-local and non-reentrant.
- **Release is `close()`**, never `LOCK_UN`. An explicit `LOCK_UN` acts on the open file
  description, so it would also unlock for any process sharing it.
- Descriptors are never passed to children (`close_fds=True`, no `pass_fds`). A process
  that shares the descriptor can close or unlock it for everybody.
- `LockLease.validate()` reads `/proc/self/fdinfo`: an exclusive lease must show
  `FLOCK ADVISORY WRITE`, a shared lease `FLOCK ADVISORY READ`. Neither mode satisfies
  the other, and a shared lease cannot be borrowed through `transition_lock`.
- `SIGKILL` of the last writer closes its descriptors, so the kernel releases the lock.
  The gate file stays; only a stale registry record may remain.

## Lock hierarchy and acquisition order

From outermost to innermost:

1. `ticket.lock` (canonical ticket serialization);
2. `03-dev/tasks/scheduler.lock` of the source generation;
3. other serialization locks (`feedback.lock`, `consultation.lock`, `limits/limits.lock`);
4. `writer.gate`, always last.

Serialization locks keep their current behaviour and receipts. Every exclusive gate
acquisition is non-blocking, and a writer's wait for `LOCK_SH` is bounded (default 30 s,
at most 300 s) and logged as contention. A transition never waits for the gate while
holding a serialization lock, so the order cannot deadlock.

**`ticket.lock` is not a quiescence proof.** `ticket_lock` performs an explicit
`LOCK_UN` when the controller leaves it, and it serializes controllers, not writers.
Holding it never substitutes for `LOCK_EX` on the gate.

## Delegated feedback handoff

Delegated feedback already holds `ticket.lock` and the source `scheduler.lock` while
its own controller is a writer. `handoff(lease, publish, held=...)`:

1. checks the serialization receipts and the writer lease;
2. closes its own `LOCK_SH`;
3. takes `LOCK_EX | LOCK_NB`; `publish(proof)` revalidates the source, publishes the
   selector and returns the new generation;
4. closes the exclusive lock and takes `LOCK_SH` again, non-blocking, on the new
   generation;
5. checks that the serialization receipts are unchanged.

If another writer is active, the handoff refuses and the lease resumes on the unchanged
generation. If publication failed after the selector moved, the old lease does not
restart. No step waits, so the handoff cannot deadlock.

## Host writers

Trusted host commands of the controller (Git, which also runs on the host in the Docker
path, and the commands of local mode) run through `run_host(lease, argv)`:

- the controller holds a live writer lease, checked before and after the command;
- the command runs in a new session, without the gate descriptor;
- after it exits, any live process left in its session is recorded as uncovered.

A transition refuses automatic fencing (`uncovered_execution`) while an uncovered
process is alive, and whenever a caller's coverage probe reports an execution tree not
covered by the gate, for example a role container. Trusted host commands must not
daemonize out of their session: Git must run with `gc.autoDetach=false` and
`maintenance.autoDetach=false`.

## Historical and non-conforming writers

The gate only excludes writers that take it. Runs started before the gate existed, and
any process that writes without a lease, are invisible to `flock`. For them:

- a transition must still use the existing evidence (`docker_quiescent`, supervisor
  records, live lock receipts) as a coverage probe, and refuse when it cannot cover an
  execution tree;
- a lease detects a selector moved by a non-conforming publisher (`stale_generation`)
  and a replaced gate (`gate_replaced`) on its next `check()`.

## Writer identity and the registry

Writer identity is **PID, boot id and start time** (`ssdlc_supervisor.Proc`), never the
PID alone. Writers record it in `.ssdlc/<ticket>/writer-gate.d/` for diagnostics and
remove the record on release. The registry never authorizes and never blocks a writer
or a transition:

- a record left by a killed writer, or a record whose PID now belongs to another
  process, is reported as stale and ignored;
- a record of a live process that does not hold the lock admits nothing;
- only a live `uncovered` record (a host descendant that escaped its lease) refuses
  automatic fencing, and only while that exact process is alive.

Contention is logged by the `ssdlc_writer_gate` logger with the kernel holders from
`/proc/locks`.

## Initialization order

`SSDLCOrchestrator.__init__` creates the phase directories and, through
`_load_or_init_state()`, `state.json` when it is missing. It also resolves the active
generation (`runtime_root`). Under this protocol the entrypoint acquires the writer
lease **before** constructing the orchestrator, so the generation read and these
creations happen under `LOCK_SH`. Without the gate (as today) they are harmless for
fencing: directory creation is idempotent, `state.json` is written only when absent, and
an authorized I2 generation without `state.json` is refused before anything is created.

## Limits

- **Linux only.** The gate needs `flock`, `/proc/self/fdinfo` and `/proc/locks`.
- **Local filesystem only.** `fstatfs` must report ext2/3/4, XFS, Btrfs, tmpfs, F2FS,
  ZFS or bcachefs. NFS, SMB/CIFS, FUSE, 9p, Ceph, cluster filesystems, overlayfs and
  unknown types are refused, because their locks are not enforced by this kernel for
  every opener.
- **Docker on the same host.** Containers are covered only through the host controller
  and coverage probes; a container on another host cannot be fenced.
- A recreated gate is detected through a bound identity or an open lease; a gate
  deleted and recreated while nobody holds or binds it cannot be told apart from a
  first creation.
