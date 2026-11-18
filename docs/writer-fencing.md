# Writer fencing protocol

Automatic fencing (#45) lets a transition prove, with a kernel lock, that no writer
of the canonical ticket is running. This page describes the protocol implemented in
`ssdlc_writer_gate.py`.

> **Status: parts (a), (b) and (c1) of #45.** The gate, its protocol and the
> in-container holder (`ssdlc_container_holder.py`, see
> [Containers](#containers-the-pid-1-holder)) exist, and the operator's host passed the
> Docker test of the holder on 2026-11-16. Part (c1) runs the role containers of
> **admitted runs** under the holder (see [Role containers](#role-containers-activation-and-admission)).
> Transitions, the supervisor and the delegated feedback handoff still use the manual
> path: the versioned proof in the contracts, historical compatibility and the crash
> tests are part (c2), #158. Host command trees can be proven finished by a child
> subreaper (#132, [Host writers](#host-writers)); `run_host()` is not yet used by
> the controller, so nothing changes for real runs.

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
   `LOCK_SH` for its whole life, or by a kernel proof that it ended. A writer whose end
   the kernel cannot prove, such as the process tree of a host command run without the
   subreaper or not yet concluded, makes the transition refuse
   (`HOST_TREE_UNVERIFIABLE`, `uncovered_execution`); the manual path stays available.
7. **Every refusal is diagnosable** through a `GateError` with a stable `code`. A refusal
   never yields a proof.

## The gate file

The gate is created once. A complete record (`ssdlc-writer-gate-v1`, holding the
gate's own inode number) is written to a temporary file and published with `link(2)`,
which never replaces an existing path, first as the anchor `writer-gate.anchor` and
then as `writer.gate`. The anchor is a second hard link to the same inode: it keeps the
canonical identity outside the gate path and marks the ticket as initialized. The inode
is created with mode `0600`, so only its owner (and root) can open it.

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
| A host command tree of the current generation is not proven finished | `HOST_TREE_UNVERIFIABLE` |
| A coverage probe reports an execution tree outside the gate | `uncovered_execution` |
| A registry entry cannot be read as a record | `registry_corrupt` |

A refused gate is never repaired automatically: an operator decides what happened.

## Descriptor lifecycle

- Gate descriptors are opened `O_RDONLY | O_CLOEXEC` and are private to the process
  and thread that opened them. They are registered as `LockLease` receipts
  (`ssdlc_locks.record_lock`), which are thread-local and non-reentrant.
- **Release is `close()`**, never `LOCK_UN`. An explicit `LOCK_UN` acts on the open file
  description, so it would also unlock for any process sharing it.
- Gate descriptors are never passed to children (`close_fds=True`; the only `pass_fds`
  is the host tree supervisor's report pipe). A process that shares the descriptor can
  close or unlock it for everybody.
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
3. refuses if a host command tree of that generation is not proven finished
   (`HOST_TREE_UNVERIFIABLE`, see [Host writers](#host-writers)), if a registry entry is
   unreadable (`registry_corrupt`), or if a coverage probe reports an uncovered
   execution tree (`uncovered_execution`);
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
cannot deadlock. A controller whose host commands of the current generation are not all
proven finished cannot hand off automatically: its transition refuses with
`HOST_TREE_UNVERIFIABLE`.

## Host writers

Trusted host commands of the controller (Git, which also runs on the host in the Docker
path, and the commands of local mode) run through `run_host(lease, argv)`:

- the controller holds a live writer lease, checked before and after the command;
- the command runs under a **host tree supervisor** (`python3 ssdlc_writer_gate.py
  --host-tree`), started in a new session and without the gate descriptor. The
  supervisor makes itself a child subreaper with `prctl(PR_SET_CHILD_SUBREAPER)`, opens
  the ticket directory and reports on a private pipe whether the mechanism is in place;
- **before the command executes**, the registry gets a host marker for the lease's
  generation (`h-<digest>.json`, one per generation) and a tree record for this command
  (`t-<tree>.json`: generation, supervisor identity, and the mechanism or the reason it
  is missing), both published with `link(2)`. The supervisor waits as a barrier for one
  line on its stdin pipe, which only the controller writes after both are published, and
  then starts the command in its own process group. If the controller dies or a record
  cannot be published first, the barrier reads EOF and the command never runs;
- the supervisor closes its copies of the caller's pipes and reaps every child with
  `waitpid(-1)` until the kernel answers `ECHILD`. Only then does it publish the
  conclusion `c-<tree>.json` (generation, its own identity, the command's status) and
  exit with the command's status. `run_host()` therefore returns when the **whole tree**
  has ended, not just the command;
- on `timeout` the controller sends `SIGTERM` to the supervisor, which kills the
  command's group and then every child it still has, including orphans reparented to it
  from other sessions, until `ECHILD`; it concludes and `TimeoutExpired` is raised;
- markers, tree records and conclusions are **never removed** by the gate, whatever
  happens to the command, its descendants, the supervisor or the controller.

**Why the subreaper is a kernel proof and a scan is not.** A descendant can fork and its
parent exit between any two reads of `/proc`, so a scan that lists PIDs and then reads
their states can miss a grandchild that is still writing (the race of PR #127).
**Process scans are never quiescence evidence**: they appear only in diagnostics
(`holders()`), and the supervisor lists its children only to kill them on timeout. A
subreaper does not look for its descendants: when a process exits, the kernel
reparents its children to the nearest subreaper ancestor *before* the exit is reported
to the parent. So when `waitpid(-1)` returns `ECHILD` to the supervisor, no process of
the tree exists, not even one that changed session or process group, and none can be
added: the tree has no member left to fork.

A transition refuses automatic fencing with `HOST_TREE_UNVERIFIABLE`, naming every
reason in the detail, unless every host tree of the current generation is proven:

| Registry state for the current generation | Detail |
| --- | --- |
| Tree record without the mechanism | `ran without a kernel mechanism: <reason>` |
| Tree record without its conclusion, supervisor alive | `no subreaper conclusion (supervisor still running)` |
| Tree record without its conclusion, supervisor gone | `no subreaper conclusion (supervisor ended without recording the proof)` |
| Conclusion of another supervisor or generation | as a missing conclusion |
| Host marker written before tree records existed (no `trees: host-tree-v1`) | `has no tree records` |
| Host marker or tree record whose generation cannot be read | `has no readable generation` |

A generation is readable only if it is the canonical encoding produced by
`generation_key()` and the record's name is the one derived from it (the marker's digest,
the tree id): a missing, empty, non-canonical or mismatched generation refuses like the
current one. If `run_host` finds its marker name already taken by anything but a valid
marker of its own generation, or its tree record name taken, it refuses
(`registry_invalid`) before the command executes. A marker with no tree record means
that no command was released past the barrier.

**If the mechanism is not available** (not Linux, `prctl` refused, or the supervisor
cannot reach the ticket directory), the command still runs, as with the plain barrier
of #115, but its tree record names the reason, and automatic fencing stays refused for
the generation; a later proven tree does not clear it. `host_tree_support()` reports the
reason checked by the controller.

**If the supervisor dies** before `ECHILD` (killed, or crashed), its orphans are
reparented to the next subreaper above it or to init, so the tree can no longer be
proven: no conclusion is ever written, and the generation stays refused even after the
remaining processes end. Only the manual path moves it. A supervisor that dies after
writing the conclusion changes nothing, since the tree had already ended.

**cgroup v2 was not chosen.** A dedicated cgroup per command, with `cgroup.events`
`populated 0`, would also be a kernel proof, but on this host it needs a cgroup
delegated to the controller's user (not available to unprivileged sessions, and under
systemd only for units with `Delegate=`), a child of the controller's own cgroup that
systemd does not manage, and, for a root controller, trust that no descendant moves
itself out through `cgroup.procs`, which root can do. The subreaper needs no privilege,
no configuration and no delegation, a descendant cannot leave it, and its failure, the
supervisor's death, fails closed.

A well-formed marker or tree record of an earlier generation no longer refuses: the
generation could only have moved through a path that accounted for every writer, the
manual one with its operator attestation that every writer is stopped. The records stay
as diagnostics.

A transition also refuses automatic fencing (`uncovered_execution`) whenever a caller's
coverage probe reports an execution tree not covered by the gate, for example a role
container.

## Containers: the PID 1 holder

A lock held by the controller or by the `docker run` client does not cover a
container: if the client dies while the create request is in flight, the daemon can
start the container after the check. The trusted holder of the lock therefore runs
**inside** the container, as its PID 1, in place of `--init`/tini:
`ssdlc_container_holder.py`, started with `--entrypoint python3` and
`holder_command()`.

Before any payload or inference, PID 1, which starts as root:

1. refuses unless it is PID 1 (`CONTAINER_HOLDER_NOT_PID1`), runs as root
   (`CONTAINER_HOLDER_NOT_ROOT`) and the host requirements below hold;
2. makes itself non-dumpable;
3. opens the gate, mounted read-only, `O_RDONLY | O_CLOEXEC`, with the identity checks
   above and without creating or writing anything (`gate_missing` if it is absent).
   The gate inode must be **root-owned 0600** (`CONTAINER_GATE_EXPOSED` otherwise);
4. takes `LOCK_SH`, with a bounded wait (`writer_wait_timeout`);
5. rereads the generation from the authoritative source, following the selectors and
   precedence of `ssdlc_transitions.runtime_root()`, and compares it with the one the
   host passed (`--generation`). A different generation refuses (`stale_generation`),
   a missing or invalid state too (`generation_invalid`): a pending or unbound
   selector, or a `state.json` of the selected runtime that is missing, not a regular
   file, not a JSON object or of another ticket.

Every refusal exits with code 122 and a line `ssdlc-holder-refused {"code": …}` on
stderr (`parse_refusal()`); the payload never starts. The holder never writes the
diagnostic registry, which stays the host's.

**UID separation instead of masking the mount** (operator decision, 2026-11-16). No
capability is added, no custom seccomp profile and no user namespace is used. PID 1
runs the payload with the unprivileged numeric UID and GID given by `--payload-user`,
after dropping the whole bounding set, clearing ambient and inheritable capabilities
and supplementary groups, and setting `no_new_privs`; it verifies the result before
`exec`. The payload cannot open the gate or its anchor through any mount, because the
inode is root-owned `0600`: it can neither take `LOCK_EX`, nor hold `LOCK_SH`, nor
release the lock. A payload that must run as root (no payload user, or UID or GID 0)
is refused with `CONTAINER_PAYLOAD_PRIVILEGED`, and the manual path stays available.

**The descriptor stays with PID 1.** It is `O_CLOEXEC`, and the child closes every
descriptor but stdio before `exec`: whoever shares the open file description could
unlock it for everybody. A payload that tries `LOCK_EX` or `LOCK_UN` on its own
descriptors does not change PID 1's lock.

PID 1 forwards `SIGTERM`, `SIGINT`, `SIGHUP`, `SIGQUIT`, `SIGUSR1`, `SIGUSR2`,
`SIGWINCH` and `SIGALRM` to the payload and reaps zombies. When the payload ends, it
kills and reaps the rest of its PID namespace, and only then releases the lock by
exiting with the payload's status.

**Exit of PID 1 is not proof that the container ended.** Linux closes PID 1's files
before it kills the rest of the namespace, and `docker kill` or processes entered with
`docker exec` escape the reaping above. After `LOCK_EX`, a transition therefore still
checks with Docker that no container of the run is created, running, paused or
restarting, through the coverage probe `running_containers(label)`. It lists only
containers with the run's label, never signals any container, and treats a Docker
failure as a refusal (`uncovered_execution`).

**Host requirements, checked at start:** Linux with a local filesystem (as above),
and no `CAP_SYS_PTRACE` in any capability set of PID 1 (`CONTAINER_PTRACE_CAPABLE`),
which is already Docker's default. Yama and seccomp are never changed globally.

**Mounts.** The ticket is mounted read-only at its host path: it holds the gate, the
anchor and the generation sources. The same inodes must not be reachable by the payload
with weaker permissions through other mounts of the repository; since the permission
belongs to the inode, the gate and its anchor are `0600` root through any path.

**Docker test.** `tests/test_container_holder_docker.py` covers a container that
outlives a controller killed with `SIGKILL` (no false exclusivity), the death of a host
process that held the lock for a container (no false proof), foreign containers (never
touched), a payload trying to reach the gate, late or stale containers, and the
refusals. It is opt-in and skipped without Docker; the operator runs it on the host:

```bash
SSDLC_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p 'test_container_holder_docker.py' -v
```

It needs root (the gate must be root-owned) and a local image with `python3`
(`SSDLC_DOCKER_TEST_IMAGE`, default `ssdlc-runner:latest`).

## Role containers: activation and admission

Part (c1) of #45 (`ssdlc_role_fencing.py`). It changes only how role containers of
admitted runs start; it activates nothing on runs already in progress.

### Admission

- A run is **admitted** only if its state was created with `"writer_fencing": "v1"`.
  The orchestrator writes the marker only when it creates `state.json` for a new run.
- **Existing runs and tickets stay on the manual path**, stopped or resumed: their
  state has no marker, and nothing adds it later. Generations prepared from a run
  without the marker inherit its state, so they stay manual too.
- `open_gate()` never admits a run: an initialized gate, or one created by an admitted
  run of the same ticket, changes nothing for a run without the marker.
- The first refusal (below) is recorded in the run's state as
  `writer_fencing_refusal` (`code`, `detail`, `refused_at`) **before** any container
  starts without the holder. From then on the run is no longer admitted, even after a
  resume or a later configuration change: one of its writers ran outside the gate.

### Payload identity

The operator sets the payload identity in `.ssdlc/project.env`:

```bash
export SSDLC_CONTAINER_PAYLOAD_USER="1000:1000"   # numeric UID:GID, never root
```

| Situation | Refusal code |
| --- | --- |
| Variable unset or empty | `CONTAINER_PAYLOAD_IDENTITY_MISSING` |
| UID or GID 0 | `CONTAINER_PAYLOAD_PRIVILEGED` |
| Not numeric `UID:GID` | `invalid_payload_user` |

### Accesses

**The operator prepares the accesses**: ownership and permissions of the worktree, the
runtime directory (`.ssdlc/.runtime`, with `CODEX_HOME`, `CLAUDE_CONFIG_DIR` and the
caches) and the credentials mounted into the container, for that identity. SSDLC
**never changes** permissions, ownership or credentials of existing paths. It only gives
the identity the directories it creates for one invocation: the role cache
(`.ssdlc/.runtime/cache/roles/invocation-*`) and the verification scratch, owned by the
identity, and the disposable workspaces, shared with its group:

- the 03-dev `developer-workspace` of the attempt, writable;
- the isolated verification checkout (`03-dev/tasks/.verification/check-*/source`),
  writable;
- the feedback snapshot (`ssdlc-feedback-*/source`), readable only and still mounted
  read-only, except its `.ssdlc/.runtime`.

A workspace keeps root as owner, so host Git still trusts the checkout. Its content
gets the payload GID with group read and search (and write where the container mounts it
read-write); its `.git` is not shared, and links and files with other hard links are not
changed. The private temporary directory around it gets group search only. Git run by
the payload would refuse a root-owned checkout as of dubious ownership: the container
gets `safe.directory` for each workspace it uses, and for nothing else, in Git's command
scope (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_VALUE_<n>`). These variables
exist only in that container; host Git configuration, global or system, is not changed.
This happens only for a container of an admitted run: other runs keep root-only
workspaces.

Before each container, the host checks every host path bind-mounted into it, and the
working directory: read and search for read-only mounts, read, write and search for
writable ones (read, and write, for files). The check runs `/bin/sh` `test` with the
payload UID and GID and no supplementary groups, as the holder runs the payload, so the
kernel judges modes, owners and ACLs. The first path the identity cannot use is refused
as `CONTAINER_PAYLOAD_ACCESS_DENIED: <path>`. Named Docker volumes and paths inside the
image are not checked: host credentials mounted under `/root` and the shared caches
`/root/.cache/uv` and `/root/.npm` are usable only if the image lets the identity reach
them, which the operator's Docker run shows.

### What the host checks, then starts

For a container of an admitted run, `EngineRunner._wrap_in_role_container()`:

1. reads the identity and checks the accesses (above);
2. opens the canonical ticket's gate, creating it only on first initialization, and
   refuses unless it is root-owned `0600` (`CONTAINER_GATE_EXPOSED`). The controller must
   therefore run as root;
3. reads the selected generation (`current_generation`) and refuses unless it is the
   one the controller runs (`stale_generation`, `generation_invalid`);
4. starts the container with `--entrypoint python3` and the holder command, without
   `--init` and without `--user`, binding the gate identity (`--gate-identity`), the
   generation and the payload identity. The canonical ticket is mounted read-only at
   its host path, also in verification containers, where the rest of `.ssdlc` stays
   masked. The payload is the usual resource wrapper with the role command.

Any refusal in steps 1–3 is printed as
`[writer-fencing] Automatic fencing refused (<code>: <detail>)`, recorded as above, and
the container starts as before, with `--init` (the manual path). A refusal of the holder
inside the container (exit 122 and `ssdlc-holder-refused`, without the
`ssdlc-holder-ready` line) means that no payload ran: the role is not dispatched
(`NotDispatched`, `ENVIRONMENT_BLOCKED`) and its exit code is never read as a result.

### Roles without a role container

A role of an admitted run that would run without a role container, because local
execution is selected (`SSDLC_EXECUTION_MODE=local`) or Docker or the runner image is
unavailable, is a writer outside the gate. Before it starts, the refusal is recorded as
above with code `ROLE_OUTSIDE_CONTAINER` (the detail names the execution mode) and
printed the same way. The role then runs as before; the run stays on the manual path,
also when it is later resumed with Docker.

Role containers are covered when an orchestrator entrypoint starts them: the pipeline
phases and the resumes (`resume_task_planning()`, `resume_pair_design()`), which
start their roles for the same run. Verification containers
of the transition commands (`continuation-prepare`, I2 limits preparation) belong to
the manual transition path and start as before.

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
- only host trees of the current generation that are not proven finished, and
  unreadable registry entries, refuse automatic fencing. Neither depends on whether a
  PID is alive: a live supervisor only changes the wording of the detail.

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
- **Docker on the same host.** Containers are covered by their PID 1 holder and by the
  Docker coverage probe; a container on another host cannot be fenced.
- **Unprivileged payloads only.** The holder needs a root PID 1 and a non-root payload
  UID and GID; a payload that needs root is refused (`CONTAINER_PAYLOAD_PRIVILEGED`).
  Role containers get it from `SSDLC_CONTAINER_PAYLOAD_USER`, and the operator gives it
  access to the files the role uses ([Accesses](#accesses)).
- **New runs only.** Runs created before part (c1), and runs refused once, stay on the
  manual path ([Admission](#admission)).
- **Host commands are covered only through `run_host()` and its subreaper** (#132).
  Without the subreaper, or with a tree not concluded, they disable automatic fencing
  for their generation. `run_host()` is not yet wired into the controller: real runs
  keep using the manual path. Work a host command delegates over IPC to another
  service (the Docker daemon, systemd, an agent socket) is not part of its tree; it
  needs its own coverage probe. The supervisor runs with the controller's identity, so
  a host command could kill it or forge a conclusion: host commands are trusted, and a
  killed supervisor fails closed. Each host command leaves two small registry records.
- A deleted or replaced gate is detected through its anchor, the registry, a bound
  identity or an open lease, and is never recreated. Only deleting the gate, its anchor
  and the whole `writer-gate.d/` registry together makes the ticket look never
  initialized; that is outside the protocol and needs an operator.
