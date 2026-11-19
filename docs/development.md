# Developing SSDLC

This guide is for anyone who changes this repository, people or agents. It covers the test suite,
the documentation tests, the CI, the rule that keeps documents in step with the code, and how
SSDLC runs on its own code. Agents also follow [AGENTS.md](../AGENTS.md).

## Setup

```bash
python3 -m venv .tools/python
.tools/python/bin/python3 -m pip install -r requirements.txt
```

devloop (`devloop/`) uses only the Python standard library, and its tests run without installing
the requirements. The local engine and its tests need them.

## Running the tests

### The whole suite

```bash
.tools/python/bin/python3 scripts/run-tests.py          # what CI runs
.tools/python/bin/python3 scripts/run-tests.py -j 4     # with 4 worker processes
```

`scripts/run-tests.py` runs exactly the tests of `python3 -m unittest discover -s tests`, split
across parallel worker processes. `-j N` sets how many (default: the number of CPUs). The modules
are split slowest first, using the durations of the previous run, kept in
`.ssdlc/test-durations.json` (ignored by git). A module that took more than 60 seconds in the
previous local run or, when that file has no entry for it, in the versioned `tests/durations.json`
(module and seconds from a full run; a fresh checkout such as CI has only this one) is split by test
class across the processes; a module in neither file stays whole; a module with
`setUpModule`/`tearDownModule` or a custom suite stays whole, and output and failures are still
reported per module. Before starting, it runs `git update-index -q --refresh` once if the root is a
git repository, so the tests' `git status` calls do not rehash every file of a fresh checkout; a
failure there is only reported. The summary at the end gives the total, the slowest
modules and the output of every failed module, each under a `FAILED MODULE` line; the exit code is
non-zero when a test failed or a worker did not finish. On 4 CPUs the suite takes about 8 minutes;
serially, about 25.

### A single module

```bash
python3 -m unittest discover -s tests -p 'test_name.py'
```

This form works for every module. `python3 -m unittest tests.test_name` needs `PYTHONPATH=tests`
for the modules that import helpers from `tests/` (`doc_contracts`, `helpers/`, `test_devloop`).
devloop runs its local tests the same way, module by module (see
[Local tests](devloop/reference.md#local-tests)).

### Opt-in and isolated checks

- Isolated A5 recovery gate, a separate CI job: `python3 -I ssdlc_replay_gate.py --output recovery-benchmark.json`.
- Docker smoke tests: `python3 tests/docker_smoke.py --image <image> --suite` and
  `python3 tests/docker_pid_smoke.py --image <image>`.
- Docker test of the writer gate holder (opt-in, as root on the host):
  `SSDLC_DOCKER_TESTS=1 python3 -m unittest discover -s tests -p 'test_container_holder_docker.py' -v`
  ([Writer fencing](writer-fencing.md#containers-the-pid-1-holder)).

## Documentation tests

Two kinds of test read the documentation. Both are part of the suite and run like any other
module.

- **Links** (`tests/test_doc_links.py`): relative links, anchors and repository paths cited in
  `README.md`, `AGENTS.md` and every page under `docs/` must resolve. An anchor must match a
  heading of the target page, with GitHub's slugs. A path in inline code is checked when it starts
  with `devloop/`, `scripts/`, `schemas/`, `docs/` or `tests/`, or names a `*.py` file at the root.
  External links and fenced code blocks are skipped; to skip an intentional example, put
  `<!-- doc-links: ignore -->` on the line before it. The allow-list of known broken references is
  empty, and adding an entry needs a review.
- **Contracts** (helpers in `tests/doc_contracts.py`, used by several `test_*.py` modules): a
  contract checks that the documentation states a real limit or non-guarantee of the system. It
  matches normalized phrases (case, inline code and emphasis markers and whitespace do not count),
  not exact formatting. `user_facing_docs()` is the README plus every page under `docs/` reachable
  from it through relative links, so a contract keeps passing when a page moves, as long as it
  stays linked; a page that nothing links does not count. `plan_section()` reads one section of a
  page, for a check bound to that section.

When you move or rename a page, keep it reachable from the README and keep the phrases its
contracts require; `tests/test_readme_structure.py` fails when a page under `docs/` or
`docs/devloop/` is not reachable.

## CI

Two workflows run on pushes to `main` and on pull requests against it:

| Workflow | Runs | When |
|---|---|---|
| `.github/workflows/test.yml` | The whole suite with `scripts/run-tests.py`, after installing the requirements, and checks that it ran as many tests as the discover finds. A second job runs the isolated recovery gate | Always |
| `.github/workflows/devloop.yml` | `python3 -m unittest -v tests.test_devloop tests.test_devloop_boundary` on Python 3.12, **without** installing the requirements: devloop must keep working with the standard library alone | Only when the change touches `devloop/`, `tests/test_devloop*.py` or the workflow itself |

The devloop workflow is fast feedback, not a replacement: the whole suite, devloop tests
included, still runs in `test.yml`. devloop never pushes changes under `.github/`
(`protected_paths`, see [GitHub identities](devloop/reference.md#github-identities)): a workflow
pushed to a branch would run in CI before anyone reviewed it, so changes to the workflows are the
operator's. A CI failure in modules unrelated to the PR may be re-run once instead of becoming a
round (see [CI failures not pertinent to the PR](devloop/reference.md#ci-failures-not-pertinent-to-the-pr)).

## Documents follow the code

`[docs.map]` (#203) maps code paths to the documents that describe them. Since ADR 0006, R3 it is
in ssdlc's [project file](devloop/reference.md#project-file), on the host: its example in the
repository is `ops/host/ssdlc/project.toml`, which a PR changes and the operator copies to the
host. A key is a directory ending in `/` (every file under it) or a single file; the entries that
cover a file add up. The rule for a change:

- **Who changes a mapped file updates its documents** when the behaviour they describe changes, in
  the same PR. devloop lists the mapped documents in the implement and fix prompts.
- **A reviewer reports a documented behaviour changed without its document as a blocking finding.**
- A file that no entry covers asks for no document.

Its format, checks and prompts are in [Documentation map](devloop/reference.md#documentation-map).

## The pinned controller

SSDLC is developed with SSDLC, so the code that runs must not be the code being changed.

- **Local engine.** When SSDLC develops itself, run a pinned controller checkout, at a reviewed
  version, against a **separate** target checkout. The run records its controller (path, Git HEAD,
  status and file hashes) and refuses to go on when it changes; changing controller requires an
  explicit migration. See [Controller contract](architecture.md#controller-contract).
- **devloop.** devloop runs from a read-only copy of one commit of `origin`, made with
  `python3 -m devloop pin <ref>` and started with `scripts/devloop-pinned`, never from the checkout
  it is changing. A merge to `main` changes nothing in the running copy until the next pin. See
  [Run from a pinned installation](devloop/guide.md#run-from-a-pinned-installation).

## Contributing

- Agents contributing to this repository follow [AGENTS.md](../AGENTS.md).
- [devloop](devloop/guide.md) dispatches issues labelled `agent:ready` to an implementer agent and
  an independent reviewer agent, up to a PR that a human merges (devloop merges only when the
  operator enables a merge mode). Its markers and records are described in the
  [protocol](devloop/protocol.md).
- The structure of the documentation is decided in
  [ADR 0003](adr/0003-struttura-della-documentazione.md).
