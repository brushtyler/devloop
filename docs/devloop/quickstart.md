# devloop: quick start for a new project

This page takes devloop from nothing to a running service for a repository that is not ssdlc and
does not contain devloop, here `acme/widget`, on a host of the operator (ADR 0006, R9). Each step
is short and links the page that explains it: the [ops guide](../../ops/README.md) for the scripts,
the [guide](guide.md#run-from-a-pinned-installation) and the [reference](reference.md) for devloop.

devloop is installed **on the host**, with a system user and a `state_dir` of the project. There is
no container for now: R7 of ADR 0006 is suspended, because codex's sandbox does not start in a
container without `--privileged` (decision of the operator of 2026-11-21, on #426).

`tests/test_devloop_quickstart.py` checks this page: the example project file and overlay load
with the installation's configuration, the commands it cites exist, and the parameters, the
overlay and the commands name the same users and paths. The first run on a real test repository,
with its apps, logins and host, is the operator's (#515).

## Before you start

The rules that hold for every project (ADR 0006, sections 3.6 and 3.8, decisions D3 and D6):

- **One repository, one host.** A repository is served by one dispatcher, on one host. The
  repository lock stops a second dispatcher of the same repository on the same host, whatever its
  user or `state_dir`; between two hosts nothing stops it, so never configure the same repository
  on two hosts ([Several projects on one host](guide.md#several-projects-on-one-host)).
- **A set of apps of its own.** The project's GitHub Apps are created from the manifests of
  `ops/github/apps/` with an `APP_PREFIX` of its own, installed on that repository only and never
  shared with another. The merger app is created only for a repository where devloop merges
  (`[merge] mode` `"strict"` or `"auto"`) ([Identities of a project](reference.md#identities-of-a-project)).
- **Users of its own.** The project has its own system user, agent user, home, clones,
  `state_dir`, `key_dir` and service; on a host that already serves ssdlc or another project,
  this is the only way ([Più progetti sullo stesso host](../../ops/README.md#più-progetti-sullo-stesso-host)).
  Its agent user has its own logins of the CLIs.
- **The operator's acts.** Creating and installing the apps (they are credentials), the logins
  of the CLIs, the project file, the overlay and starting the service are the operator's.

What you need:

- a host like the verified one (Ubuntu with `apt` and `systemd`, sudo 1.9 and not sudo-rs:
  [Piattaforma verificata](../../ops/README.md#piattaforma-verificata)), and `sudo` on it;
- a checkout of ssdlc of the operator on the host, from which every command of this page runs:
  `ops/` is there;
- admin rights on `acme/widget`, to install the apps;
- a tag of ssdlc to pin: the installation comes from that tag (the
  [installation's source](guide.md#installation-source)), not from the managed repository.

The examples use `PROJECT=widget`, from which the scripts derive every name:

| What | Value |
|---|---|
| devloop's user and its agent user | `devloop-widget`, `devloop-widget-agent` |
| clone of the managed repository | `/home/devloop-widget/widget` |
| clone of ssdlc, the installation's source | `/home/devloop-widget/devloop-source` |
| `state_dir`, with the project file, the overlay, the pinned copies and `run.log` | `/var/lib/devloop/widget` |
| `key_dir` of devloop's user | `/home/devloop-widget/.ghapps` |
| launcher, installed from the tag | `/usr/local/lib/devloop/devloop-pinned` |
| service | `devloop@widget` |

## 1. Parameters

One parameters file for the project, in the checkout of ssdlc (git ignores it; no secret goes
there). Every other value is derived from these
([Regole degli script](../../ops/README.md#regole-degli-script), `ops/params.env.example`):

<!-- quickstart: params -->
```bash
# ops/params-widget.env
REPO=acme/widget
DEFAULT_BRANCH=main
GITHUB_OWNER_TYPE=org
APP_PREFIX=acme-widget-devloop-
PROJECT=widget
SOURCE_REPO=brushtyler/ssdlc
SOURCE_REF=v1.2.0
STATE_DIR=/var/lib/devloop/widget
# No merger: devloop does not merge here ([merge] mode stays "off").
APPS="dispatcher implementer claude-agent codex-agent agy-agent supervisor"
DEVLOOP_KEYS="dispatcher implementer claude-agent codex-agent agy-agent"
```

- `SOURCE_REPO` and `SOURCE_REF`: devloop comes from that tag of ssdlc.
- `APPS` are the apps `ops/github/create-apps.sh` prepares, `DEVLOOP_KEYS` those whose key goes to
  devloop's `key_dir`: the dispatcher, the implementer (the installation's agents are named by
  role, and `claude-implementer` signs with it) and the reviewers' apps `claude-agent`,
  `codex-agent` and `agy-agent`. The supervisor's `key_dir` gets the keys of `SUPERVISOR_KEYS`
  (step 7).
- For a merge mode other than `"off"`, add `merger` to both lists, and `[apps.merger]` to the
  overlay ([The merger app](reference.md#the-merger-app)).

## 2. Host

```bash
sudo bash ops/host/setup-devloop.sh --params ops/params-widget.env --dry-run   # what it would do
sudo bash ops/host/setup-devloop.sh --params ops/params-widget.env
```

It installs the missing packages and creates `devloop-widget`, the clone of `acme/widget`, the
clone of ssdlc, the launcher from the tag `SOURCE_REF`, the `state_dir` with `run.log` and a fully
commented overlay, the `key_dir`, the template unit `devloop@.service` with the drop-in of
`devloop@widget`, and the directory and group of the repository locks. It does not start the
service, and it lists what is left to do by hand
([4. Host di devloop](../../ops/README.md#4-host-di-devloop),
[Un altro progetto](../../ops/README.md#un-altro-progetto)).

## 3. GitHub Apps

```bash
bash ops/github/create-apps.sh --params ops/params-widget.env --dry-run   # the apps and their permissions
bash ops/github/create-apps.sh --params ops/params-widget.env --out ~/create-apps.html
```

Open the page in a browser with the GitHub session of the owner of `acme/widget`. For each app,
as in [2. GitHub App](../../ops/README.md#2-github-app) of the ops guide:

1. create it from its button;
2. exchange the `code` within one hour, keeping only `id`, `slug` and the private key, never
   printed nor copied anywhere else;
3. install it: *Install App* → *Only select repositories* → `acme/widget` alone;
4. put its key in the `key_dir` of the user that uses it, mode `600`: devloop's apps in
   `/home/devloop-widget/.ghapps`, those of `SUPERVISOR_KEYS` in the supervisor's (step 7).

```bash
sudo install -o devloop-widget -g devloop-widget -m 600 <slug>.<date>.private-key.pem /home/devloop-widget/.ghapps/
```

Note each `app_id` and `slug`: they go to the overlay (step 4). A ruleset of the base branch, with
the checks of the project's CI, is optional
([3. Ruleset del branch principale](../../ops/README.md#3-ruleset-del-branch-principale)).

## 4. Project file and overlay

Two files of the operator, in the `state_dir`, which devloop never reads from the repository
([Project file](reference.md#project-file), [Local config as an overlay](guide.md#local-config-as-an-overlay)).

**The project file**, `/var/lib/devloop/widget/project.toml`: the facts of the repository and
the restrictions it adds. `test_cmd` is required; every other key is optional, and a key outside
its schema stops devloop, naming it. For example:

<!-- quickstart: project.toml -->
```toml
# The command of the whole test suite, run by the dispatcher in the worktree.
test_cmd = "python3 -m pytest -q"
# "full" runs test_cmd after each writing turn. "changed" needs a selection in [tests]
# (docs/devloop/reference.md, "Local tests"); without one it is "full".
local_tests = "full"
# The language of the text the agents write for GitHub.
language = "English"
# Never pushed by devloop: a workflow pushed to a branch would run before any review.
protected_paths = [".github/"]

# The project's data in the prompts (docs/devloop/reference.md, "Project data in the prompts").
[prompts]
instructions = ['`CONTRIBUTING.md`']
fix_instructions = ['`CONTRIBUTING.md`']
module_test_cmd = "python3 -m pytest -q tests/test_name.py"

# Code paths and the documents that describe them (docs/devloop/reference.md,
# "Documentation map").
[docs.map]
"src/" = ["docs/usage.md"]
```

```bash
sudo install -o devloop-widget -g devloop-widget -m 600 project.toml /var/lib/devloop/widget/project.toml
```

**The overlay**, `/var/lib/devloop/widget/config.local.toml`: the identities, the trust and every
key the operator sets. `setup-devloop.sh` wrote it fully commented, and it is `devloop-widget`'s:
edit it as that user (`sudo -u devloop-widget -H "${EDITOR:-vi}" /var/lib/devloop/widget/config.local.toml`).
For example, with the `app_id` and `slug` of step 3:

<!-- quickstart: config.local.toml -->
```toml
# The managed repository and the logins whose labels and markers count.
repo = "acme/widget"
trusted_users = ["acme-operator"]
# The state_dir the launcher gets with --state-dir, and the clone of ssdlc pins come from.
state_dir = "/var/lib/devloop/widget"
source = "/home/devloop-widget/devloop-source"
# The directory of the repository locks, the same for every project of the host.
repo_lock_dir = "/run/devloop/repos"

# The agents and the tests run as this user (docs/devloop/guide.md, "Agent user").
[agent_user]
name = "devloop-widget-agent"

# agy runs only these commands: the overlay alone admits the project's test command.
[agents.agy-reviewer]
write_commands = ["python3 -m pytest", "git status", "git diff", "git log", "git show"]
review_commands = ["python3 -m pytest", "git status", "git diff", "git log", "git show"]

# The project's set of apps, installed on acme/widget only, with its keys.
[apps]
key_dir = "/home/devloop-widget/.ghapps"

[apps.dispatcher]
app_id = 1000001
slug = "acme-widget-devloop-dispatcher"

[apps.implementer]
app_id = 1000002
slug = "acme-widget-devloop-implementer"

[apps.claude]
app_id = 1000003
slug = "acme-widget-devloop-claude-agent"

[apps.codex]
app_id = 1000004
slug = "acme-widget-devloop-codex-agent"

[apps.agy]
app_id = 1000005
slug = "acme-widget-devloop-agy-agent"

# Declared only: it counts once [roles.supervisor] is a table (docs/devloop/reference.md,
# "The supervisor app"). devloop never reads its key.
[apps.supervisor]
app_id = 1000006
slug = "acme-widget-devloop-supervisor"
```

- `repo`, `state_dir`, `source` and `repo_lock_dir` are those the setup script reminds; without
  `repo` and `trusted_users` devloop does not start.
- `[merge] mode` is left at `"off"`: devloop merges nothing, and every ready PR is the
  operator's ([Merge modes](guide.md#merge-modes)). Other keys of the operator, such as
  `[run] alert_issue`, are in [Starting and stopping the loop](guide.md#starting-and-stopping-the-loop).

## 5. Agent user and CLI logins

```bash
sudo bash ops/host/setup-agent-user.sh --params ops/params-widget.env --dry-run
sudo bash ops/host/setup-agent-user.sh --params ops/params-widget.env
sudo bash ops/host/setup-agent-user.sh --params ops/params-widget.env --unseal
sudo -iu devloop-widget-agent        # install claude, codex and agy in ~/.local/bin, log in to each
sudo bash ops/host/setup-agent-user.sh --params ops/params-widget.env
```

The script creates `devloop-widget-agent`, its ACLs on the `state_dir` and the worktrees and its
sudoers rule. `--unseal` opens the directories of the CLIs for the installation and the logins;
the run without it makes them root's again, and devloop does not start until it does. Run it
after step 4, so that the ACLs cover the files already in the `state_dir`. How to install each
CLI and check it with `--version`: [5. Utente degli agent e CLI](../../ops/README.md#5-utente-degli-agent-e-cli)
and [Agent CLIs](guide.md#agent-clis).

## 6. First pin

The first pin runs devloop of the clone of ssdlc, in the clone of the managed repository, with the
project file and the overlay before the command; it ends with the smoke test (`tick --dry-run` and
one round of `watch` from the new copy) ([Pin](guide.md#pin)):

```bash
sudo -u devloop-widget -H sh -c 'cd ~/widget && PYTHONPATH=~/devloop-source python3 -P -m devloop \
    --project-config /var/lib/devloop/widget/project.toml \
    --local-config /var/lib/devloop/widget/config.local.toml pin v1.2.0'
```

`devloop: invalid config: …` names the key to fix in step 4.

## 7. Supervisor

The supervisor is the same user for every project of the host, with a sudoers, a clone and a
`key_dir` per project (`/home/supervisor/.ghapps-widget`). That `key_dir` gets the keys of
`SUPERVISOR_KEYS` (by default `supervisor` and, until `[roles.supervisor]` is a table in the
overlay, `claude-agent`), mode `600`, owned by `supervisor`
([7. Host del supervisore](../../ops/README.md#7-host-del-supervisore)):

```bash
sudo bash ops/host/setup-supervisor.sh --params ops/params-widget.env --dry-run
sudo bash ops/host/setup-supervisor.sh --params ops/params-widget.env
```

## 8. Checks

```bash
sudo bash ops/host/check-host.sh --params ops/params-widget.env
sudo -u devloop-widget -H /usr/bin/env -C /home/devloop-widget/widget /usr/bin/python3 /usr/local/lib/devloop/devloop-pinned --state-dir /var/lib/devloop/widget apps
sudo -u devloop-widget -H /usr/bin/env -C /home/devloop-widget/widget /usr/bin/python3 /usr/local/lib/devloop/devloop-pinned --state-dir /var/lib/devloop/widget tick --dry-run
```

- `check-host.sh` exits with 1 on an error: users, modes, keys, ACLs, sudoers, the agent user,
  the repository lock, the unit, the CLIs and `devloop apps`
  ([8. Verifica](../../ops/README.md#8-verifica)).
- `apps` names `acme/widget` and the `key_dir`, then each app with its installation and its
  permissions ([GitHub identities](reference.md#github-identities)).
- `tick --dry-run` plans a tick from the pinned copy, without agents, without writes and without
  any lock.

Then the labels devloop uses: `setup` creates them on the repository, with the dispatcher's app.

```bash
sudo -u devloop-widget -H /usr/bin/env -C /home/devloop-widget/widget /usr/bin/python3 /usr/local/lib/devloop/devloop-pinned --state-dir /var/lib/devloop/widget setup
```

## 9. Start

Starting is the operator's act: the loop spends the agents' quotas.

```bash
sudo systemctl start devloop@widget
sudo systemctl enable devloop@widget    # to start again at boot
sudo -u devloop-widget -H tail -f /var/lib/devloop/widget/run.log
```

devloop works on the issues of `acme/widget` that a user of `trusted_users` labels `agent:ready`
([The loop](guide.md#the-loop)). To stop after the tick in progress, create
`/var/lib/devloop/widget/STOP` as `devloop-widget`, or `sudo systemctl stop devloop@widget`
([Continuous loop](guide.md#continuous-loop)).

## Later

- **A new version of devloop** is a new tag of ssdlc, pinned by hand: the pin after a merge is
  off when the installation has its own source. Pin with the launcher (`pin <tag>` instead of
  `apps` in the commands of step 8), then restart the service
  ([Installation source](guide.md#installation-source), [Rollback](guide.md#rollback)).
- **A change of the project file or of the overlay** is read again before each tick; most keys
  need a restart ([Changes while the loop runs](guide.md#changes-while-the-loop-runs)).
- **Updating a CLI** is done by hand with the loop stopped ([Agent CLIs](guide.md#agent-clis)).
