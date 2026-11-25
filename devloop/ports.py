"""The ports the dispatcher reaches its tracker, its code review and its CI through (ADR 0001).

``Tracker`` holds the work units and their history, comments and labels; ``CodeReview`` the pull
requests and their reviews; ``CI`` the checks of a commit. Each port has only the methods the
dispatcher (with its ``Ledger`` and ``ensure_labels``) calls today, with their current names and
signatures, and returns data already normalized: PR authors in REST form (``author_login``), the
merge state as ``clean``, ``conflict`` or ``unknown``, checks as ``github.normalize_checks``
returns them and summarized by :func:`summarize_checks`.

``github.GitHub`` implements all three, and ``apps.Identities.github`` returns one for each
actor: those stay the concrete adapters. ``Backend`` is the three ports together, as the
dispatcher takes them. The ports raise ``github.GitHubError`` when the backend fails.

``Tracker.comment`` and ``Tracker.edit_comment`` tell three outcomes apart (#364, ADR 0001
"Porta del tracker"): success, failure (``github.GitHubError``: nothing was written) and
**uncertain** (``github.GitHubUncertain``, a ``GitHubError``: a timeout, a connection lost
after sending, a 5xx answer, so the write may have taken effect). :func:`reconcile_comment`
tells, by reading the comments back, whether an uncertain write took effect. The outbox of the
publications (``outbox.Outbox``, #385) calls it before any new POST when ``publication_outbox``
is on; without it the dispatcher handles an uncertain outcome as a failure, as it did before.

Where a method went, when it was not obvious:

- ``pr_comments``, ``label_events``, ``labeled_events`` and ``labelers`` are in ``Tracker``
  although they also read pull requests: comments and labels are tracker state, shared by issues
  and pull requests.
- ``edit_comment`` is in ``Tracker`` next to ``comment``: the ledger edits its own records.
- ``pr_reviews`` (the native reviews) is in ``CodeReview``, and ``check_runs`` in ``CI``. Since
  #325 the native review of an agent with its own app is its review record: the dispatcher
  reads it together with ``pr_comments``, in the order of their times.
- ``rerun_failed_jobs`` (#200) is in ``CI``: the dispatcher re-runs the failed jobs of a run
  whose failed test modules are not pertinent to the PR, with a token limited to
  ``actions: write``.
- ``job_log`` (#300) is in ``CI``: the review and fix prompts summarize the CI of the head, with
  the final lines of ``scripts/run-tests.py`` read from the log of each job.
- ``commit_date`` and ``required_checks`` (#263) are in ``CI``: a head without CI waits for it
  from its push, or its commit date, and as long as the base branch requires checks.
- ``merge_state`` is a ``CodeReview`` method: the adapter turns the mergeability of a PR, as
  ``open_prs`` reports it, into ``clean``, ``conflict`` or ``unknown``.
- ``pr_state`` and ``merge_pr`` (#172) are in ``CodeReview``: the merge of a ready PR re-reads
  it and merges it. ``pr_state`` keeps ``mergeable`` and ``mergeStateStatus`` as GitHub reports
  them, because the ``strict`` merge mode requires exactly ``MERGEABLE`` and ``CLEAN``.
  ``merge_pr`` runs on the dispatcher's ``merger``: the ``merger`` app when configured (#201),
  otherwise the local login.
- ``close_issue`` (#228) is in ``Tracker`` and ``merged_prs`` in ``CodeReview``: after a merge
  the dispatcher closes the PR's issue itself, and the planner never starts an issue that a
  merged devloop PR already resolved.
- ``find_pr`` (#362) is in ``CodeReview``: the dispatcher reads, in any state, the PR of a
  legacy turn imported from ``ledger-open-turns.json`` that is not among the open ones, to
  link the turn to the issue of its work.
- ``branch_prs`` (#384) is in ``CodeReview``: with ``attempt_identity``, before resuming an
  attempt without a recorded PR, the dispatcher looks on its branch for a PR already published
  for it, in any state.
- ``commit_prs`` (#473) is in ``CodeReview``: with ``[pin] auto``, before an auto-pin the
  dispatcher reads the PRs merged by each new commit of the base branch, so that a merge it did
  not make that declares a step on the host holds the pin as one of its own.
- ``issue_dependencies`` (#277) is in ``Tracker``: the native "blocked by" dependencies between
  issues, which the planner reads to hold back an issue while a trusted block is open.
  ``issue_target`` and ``add_blocked_by`` (#672) are there too: the dispatcher adds a missing
  prerequisite named by its number as a "blocked by" of the issue, once it is an open issue of
  the repository.
- ``repo_labels`` and ``rename_label`` (#248) are in ``Tracker`` next to ``ensure_label``:
  ``ensure_labels`` renames ``agent:needs-operator`` from its old name, and ``devloop status``
  reports when both names exist.
- ``GitHub.last_labeler`` is not in any port: the dispatcher no longer calls it.
- ``open_issue_numbers`` and ``comments_since`` (#656) are in ``Tracker``: with
  ``[roles.coordinator]`` the dispatcher reads the threads of every open issue at its first
  snapshot, then the comments written since its previous snapshot, to find the coordinator's
  ``/devloop authorize`` on an issue whatever its labels. The ``watch`` command calls
  ``comments_since`` too.
- ``GitHub.open_issues``, ``issue_state``, ``issue_author`` and ``workflow_runs`` (#615) are not
  in any port: only the ``watch`` command (``devloop/watch.py``) calls them, not the dispatcher.
  Nor is ``issue_labels`` (#676), which the ``watch`` command alone calls too.
- ``GitHub.dated_comments``, ``search_prs`` and ``edit_issue_body`` are not in any port either:
  only the daily summary (``devloop/report.py``, #256) calls them, with ``issue_history``.
"""

import re
from typing import Protocol, runtime_checkable

_RUN_LINK = re.compile(r'/actions/runs/(\d+)')


@runtime_checkable
class Tracker(Protocol):
    """Work units (issues) with their history, the comments of issues and pull requests, labels."""

    # Reads ---------------------------------------------------------------
    def issues_with_label(self, label):
        """The open issues with ``label``, as ``{number, title, labels}``."""

    def open_issue_numbers(self):
        """The numbers of every open issue, pull requests excluded, without a limit on their
        count (#656)."""

    def issue(self, number):
        """Issue ``number`` as ``{number, title, body, labels, comments, state}``."""

    def issue_history(self, number):
        """Title, body and who changed them: ``{title, body, author, last_edited_at, editor,
        labeled, renamed, complete}``, ``complete`` True only when every event was read (#483,
        see ``GitHub.issue_history``)."""

    def issue_comments(self, number):
        """The comments of an issue, oldest first, as ``{id, author: {login}, body,
        created_at, updated_at}`` (``created_at`` and ``updated_at``, the latest change, #472,
        in ISO 8601, UTC, as GitHub writes them)."""

    def pr_comments(self, number):
        """The conversation comments of a pull request, as :meth:`issue_comments`."""

    def comments_since(self, since):
        """The conversation comments of every issue and pull request created or edited at
        ``since`` (ISO 8601, UTC) or later, as ``{id, login, number, body, pull_request}``, the
        login in REST form (#656)."""

    def issue_dependencies(self, number):
        """The issues that block issue ``number`` and who added each block (#277): ``{author,
        blocked_by: [{repo, number, state, state_reason, prs}], added: [{repo, number, by,
        at}]}`` (``state_reason`` and ``prs``, its open linked PRs as ``{number, author}``,
        since #671; see ``GitHub.issue_dependencies``)."""

    def issue_target(self, number):
        """``{id, state, pull_request}`` of issue or PR ``number`` (#672): the id the "blocked
        by" API takes, OPEN or CLOSED, and whether it is a PR. ``GitHubNotFound`` when it does
        not exist."""

    def labelers(self, number, label):
        """Logins of every ``labeled`` event of ``label`` on ``number``, oldest first."""

    def label_events(self, number, label):
        """The ``labeled`` and ``unlabeled`` events of ``label`` as ``{event, by, at}``."""

    def labeled_events(self, number):
        """Every ``labeled`` event on ``number``, oldest first, as ``{label, by, at}``."""

    # Writes --------------------------------------------------------------
    def edit_labels(self, number, add=(), remove=()):
        """Add and remove labels; removing a label that is not there is not an error."""

    def comment(self, number, body):
        """Post a comment; returns its id, or None if unknown. Raises ``GitHubUncertain`` when
        the outcome is unknown (#364)."""

    def close_issue(self, number):
        """Close issue ``number`` as completed."""

    def add_blocked_by(self, number, issue_id):
        """Add to issue ``number`` the native "blocked by" of the issue with id ``issue_id``
        (``issue_target``, #672)."""

    def edit_comment(self, comment_id, body):
        """Replace the body of comment ``comment_id``. Raises ``GitHubUncertain`` when the
        outcome is unknown (#364)."""

    def ensure_label(self, name, color, description):
        """Create or update label ``name``."""

    def repo_labels(self):
        """The names of the repository's labels."""

    def rename_label(self, name, new_name, color, description):
        """Rename label ``name`` to ``new_name``, with that color and description: the issues
        and pull requests that have it keep it."""


@runtime_checkable
class CodeReview(Protocol):
    """Pull requests and their reviews."""

    def open_prs(self):
        """The open pull requests, with ``author`` in REST form (``author_login``)."""

    def merged_prs(self, branch):
        """The merged pull requests from head branch ``branch``, as :meth:`open_prs` with
        ``mergeCommit``."""

    def find_pr(self, number):
        """Pull request ``number`` in any state, as :meth:`open_prs` (the mergeability fields
        aside) with ``state`` and ``mergeCommit``, or None when ``number`` is an issue."""

    def commit_prs(self, sha):
        """The numbers of the merged pull requests GitHub associates with commit ``sha``."""

    def branch_prs(self, branch):
        """The pull requests from head branch ``branch`` in any state, as :meth:`open_prs`
        (the mergeability fields aside) with ``state`` and ``createdAt``."""

    def merge_state(self, pr):
        """``clean``, ``conflict`` or ``unknown`` for ``pr`` as :meth:`open_prs` returns it."""

    def pr_head(self, number):
        """The current head commit of PR ``number``, read fresh."""

    def pr_state(self, number):
        """PR ``number`` as a merge needs it, read fresh: ``{state, headRefOid, mergeable,
        mergeStateStatus, labels, mergeCommit}``."""

    def merge_pr(self, number, head):
        """Merge PR ``number`` with a merge commit, only if its head is still ``head``."""

    def pr_reviews(self, number):
        """The native reviews of PR ``number``, oldest first, as ``{id, login, state, body,
        submitted_at}``: the body of an agent's review is its devloop record (#325)."""

    def create_pr(self, *, base, head, title, body):
        """``(number, url)`` of the new pull request."""

    def submit_review(self, number, *, commit, event, body):
        """A native review (APPROVE, REQUEST_CHANGES or COMMENT) on ``commit``."""


@runtime_checkable
class CI(Protocol):
    """The checks of a commit."""

    def checks(self, sha):
        """``summarize_checks(self.check_details(sha))``."""

    def check_details(self, sha):
        """Every check of ``sha`` as ``{name, bucket, link, conclusion, run_id, duration_s}``."""

    def check_runs(self, sha):
        """Every check run of ``sha``, superseded ones included, as ``{id, name, app_id, status,
        conclusion}``."""

    def failed_log(self, run_id, max_lines=80, job_id=None):
        """The last ``max_lines`` of the failed steps of run ``run_id`` (all of them with None),
        or only of its job ``job_id`` when given; raises ``LogUnreadable`` with the reason when
        the log cannot be read (#545)."""

    def job_log(self, job_id):
        """The whole log of GitHub Actions job ``job_id`` (#300), or None when unreadable."""

    def rerun_failed_jobs(self, run_id):
        """Re-run the failed jobs of run ``run_id`` (#200); raises ``GitHubError`` if refused."""

    def commit_date(self, sha):
        """The committer date of commit ``sha`` as ``YYYY-MM-DDTHH:MM:SSZ``, or None (#263)."""

    def required_checks(self, branch):
        """The names of the checks that the rulesets and the branch protection of ``branch``
        require, sorted (#263); empty when none does."""


@runtime_checkable
class Backend(Tracker, CodeReview, CI, Protocol):
    """The three ports together: what the dispatcher acts with, for itself and for each actor."""


PORTS = (Tracker, CodeReview, CI)


def port_methods(port):
    """The names of the methods ``port`` declares, in declaration order."""
    return [name for name, value in vars(port).items()
            if callable(value) and not name.startswith('_')]


def reconcile_comment(tracker, number, author, body, *, comment_id=None, marker=None,
                      exclude=()):
    """Whether a write on issue or PR ``number`` that raised ``github.GitHubUncertain`` took
    effect (#364): the comment that carries it, or None when the write was not published.

    Only a comment of ``author``, the login that wrote, counts: an identical comment of anyone
    else never does. ``author`` may also be a collection of logins, when the writer is known
    only as one of them (#385: the ambient ``gh`` login, one of the trusted users). A comment
    carries the write when its body is exactly ``body`` or, with a non-empty ``marker``,
    contains ``marker``. For a ``comment``, the oldest such comment is returned; for an
    ``edit_comment``, ``comment_id`` restricts the search to the edited comment. A comment whose
    id is in ``exclude`` never counts: the outbox (#385) passes the comments it already knows
    as the publication of another write.

    The answer is only as precise as what identifies the write: an identical body that
    ``author`` posted before also counts, and so does a marker that the comment carried before
    an edit. Give a ``marker`` unique to the write when either can happen. A failed read
    raises ``github.GitHubError``, and the outcome stays uncertain."""
    authors = {author} if isinstance(author, str) else set(author)
    excluded = set(exclude)
    for comment in tracker.issue_comments(number):
        if (comment.get('author') or {}).get('login') not in authors:
            continue
        if comment_id is not None and comment.get('id') != comment_id:
            continue
        if comment.get('id') is not None and comment.get('id') in excluded:
            continue
        text = comment.get('body') or ''
        if text == body or (marker and marker in text):
            return comment
    return None


def summarize_checks(checks):
    """``(state, failed_names, failed_run_ids)`` of checks in the form of ``CI.check_details``.

    ``state`` is pass, fail, pending or none; the run ids are those :meth:`CI.failed_log` reads,
    taken from the links of the failed checks."""
    if not checks:
        return 'none', [], []
    failed = [c for c in checks if c.get('bucket') == 'fail']
    if failed:
        run_ids = sorted({m.group(1) for c in failed if (m := _RUN_LINK.search(c.get('link') or ''))})
        return 'fail', [c['name'] for c in failed], run_ids
    if any(c.get('bucket') == 'pending' for c in checks):
        return 'pending', [], []
    return 'pass', [], []
