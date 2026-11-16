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
   `LOCK_EX`. Files, records, PIDs and process scans never stand in for the lock.
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
6. **Automatic fencing applies only when every writer is covered by the gate**, holding
   `LOCK_SH` for its whole life. A writer whose end the kernel cannot prove, such as the
   process tree of a host command, makes the transition refuse
   (`HOST_TREE_UNVERIFIABLE`, `uncovered_execution`); the manual path stays available.
7. **Every refusal is diagnosable** through a `GateError` with a stable `code`. A refusal
   never yields a proof.

## The gate file

The gate is created once. A complete record (`ssdlc-writer-gate-v1`, holding the
gate's own inode number) is written to a temporary file and published with `link(2)`,
which never replaces an existing path, first as the anchor `writer-gate.anchor` and
then as `writer.gate`. The anchor is a second hard link to the same inode: it keeps the
canonical identity outside the gate path and marks the ticket as initialized.

Creation happens only on **first initialization**: under a short `flock` on a private
descriptor of the ticket directory (bounded wait, 5 s), with neither the gate, the
anchor nor the registry `writer-gate.d/` present. Concurrent creators serialize on that
lock and converge on the same file. After initialization a missing gate is refused
(`gate_missing`) and never recreated, even when a writer still holds the old inode and
the caller binds no identity.

Every opening verifies the gate's identity:

- the ticket directory and the gate are opened with `O_NOFOLLOW`, relative to the
  ticket directory descriptor;
- device and inode of the open descriptor must equal those of the path (`fstat` against
  `lstat`), both before and after the lock is acquired;
- the inode recorded in the gate must equal the open inode, so a copy of the gate is
  not the gate;
- the anchor must be a regular file naming the same device and inode;
- a caller may bind the identity it saw earlier (`expected_identity`); a different
  identity is refused.

| Situation | Refusal code |
| --- | --- |
| Gate path is a symlink | `gate_symlink` |
| Gate path names another file (replaced, copied, recreated) or not the anchored inode | `gate_replaced` |
| Gate path deleted after initialization | `gate_missing` |
| Anchor deleted while the gate remains | `gate_anchor_missing` |
| Initialization lock held for more than 5 s, or a name created outside it | `gate_init_contended`, `gate_init_conflict` |
| Gate path is a directory, FIFO or device | `gate_not_regular` |
| Gate file is empty or holds a foreign record | `gate_corrupt` |
| Ticket directory is a symlink or a relative path | `ticket_root_invalid` |
| Filesystem without host-local kernel flock | `unsupported_filesystem` |
| A host command ran in the current generation | `HOST_TREE_UNVERIFIABLE` |
| A coverage probe reports an execution tree outside the gate | `uncovered_execution` |
| A registry entry cannot be read as a record | `registry_corrupt` |

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

The ticket-directory lock used for first initialization is taken only while neither
`writer.gate` nor its anchor is fully published, is never held together with the gate
lock, and is released before the gate is opened.

Serialization locks keep their current behaviour and receipts. Every exclusive gate
acquisition is non-blocking, and a writer's wait for `LOCK_SH` is bounded (default 30 s,
at most 300 s) and logged as contention. A transition never waits for the gate while
holding a serialization lock, so the order cannot deadlock.

**`ticket.lock` is not a quiescence proof.** `ticket_lock` performs an explicit
`LOCK_UN` when the controller leaves it, and it serializes controllers, not writers.
Holding it never substitutes for `LOCK_EX` on the gate.

## Transitions

`transition(ticket_root, read_generation, coverage=...)`:

1. opens and verifies the gate, then takes `LOCK_EX | LOCK_NB` (`writers_active` if a
   writer holds `LOCK_SH`);
2. verifies the gate again and reads the current generation under the lock;
3. refuses if a host marker names that generation (`HOST_TREE_UNVERIFIABLE`), if a
   registry entry is unreadable (`registry_corrupt`), or if a coverage probe reports an
   uncovered execution tree (`uncovered_execution`);
4. yields the proof (`writer-gate-exclusive-v1`) to the caller, which revalidates the
   source and publishes the selector, then closes the descriptor.

## Delegated feedback handoff

Delegated feedback already holds `ticket.lock` and the source `scheduler.lock` while
its own controller is a writer. `handoff(lease, publish, held=...)`:

1. checks the serialization receipts and the writer lease;
2. closes its own `LOCK_SH`;
3. takes `LOCK_EX | LOCK_NB` through `transition`, checks that the generation is still
   the lease's one, and calls `publish(proof)`, which revalidates the source, publishes
   the selector and returns the new generation;
4. closes the exclusive lock and takes `LOCK_SH` again, non-blocking, on the new
   generation;
5. checks that the serialization receipts are unchanged.

If another writer is active, or the transition refuses for any other reason, the
handoff refuses and the lease resumes on the unchanged generation. If publication failed
after the selector moved, the old lease does not restart. No step waits, so the handoff
cannot deadlock. A controller that ran host commands in the current generation cannot
hand off automatically: its transition refuses with `HOST_TREE_UNVERIFIABLE`.

## Host writers

Trusted host commands of the controller (Git, which also runs on the host in the Docker
path, and the commands of local mode) run through `run_host(lease, argv)`:

- the controller holds a live writer lease, checked before and after the command;
- the command runs in a new session, without the gate descriptor;
- **before the command executes**, the registry gets a host marker for the lease's
  generation (`h-<digest>.json`, one per generation, published with `link(2)`). The
  child starts as a `/bin/sh` barrier that waits for one line on its stdin pipe, which
  only the controller writes after the marker is published, and then `exec`s the
  command. If the controller dies or the marker cannot be published first, the barrier
  reads EOF and exits without executing anything;
- the marker is **never removed** by the gate, whatever happens to the command, its
  descendants or the controller.

No kernel mechanism used here proves that a host command's process tree has ended: a
descendant can fork and its parent exit between any two reads of `/proc`, so a scan
that lists PIDs and then reads their states can miss a grandchild that is still
writing. **Process scans are never quiescence evidence**: they appear only in
diagnostics (`holders()`). A transition therefore refuses automatic fencing with
`HOST_TREE_UNVERIFIABLE` whenever the current generation has a host marker, or a marker
whose generation cannot be read, even after the command and every process it started
are gone. The manual path stays available. Covering host trees with a kernel mechanism
(cgroup v2 `cgroup.events` `populated 0`, or a subreaper) is issue #132.

A marker of an earlier generation no longer refuses: the generation could only have
moved through a path that accounted for every writer, the manual one with its operator
attestation that every writer is stopped. The marker stays as a diagnostic.

A transition also refuses automatic fencing (`uncovered_execution`) whenever a caller's
coverage probe reports an execution tree not covered by the gate, for example a role
container.

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
remove the record on release. Writer records never authorize and never block a writer
or a transition:

- a record left by a killed writer, or a record whose PID now belongs to another
  process, is reported as stale and ignored;
- a record of a live process that does not hold the lock admits nothing;
- only host markers of the current generation, and unreadable registry entries, refuse
  automatic fencing. Neither depends on whether a PID is alive.

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
- **Host commands disable automatic fencing for their generation** until #132 covers
  their process trees with a kernel mechanism.
- A deleted or replaced gate is detected through its anchor, the registry, a bound
  identity or an open lease, and is never recreated. Only deleting the gate, its anchor
  and the whole `writer-gate.d/` registry together makes the ticket look never
  initialized; that is outside the protocol and needs an operator.
