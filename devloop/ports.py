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

Where a method went, when it was not obvious:

- ``pr_comments``, ``label_events``, ``labeled_events`` and ``labelers`` are in ``Tracker``
  although they also read pull requests: comments and labels are tracker state, shared by issues
  and pull requests.
- ``edit_comment`` is in ``Tracker`` next to ``comment``: the ledger edits its own records.
- ``pr_reviews`` (the native reviews) is in ``CodeReview``, and ``check_runs`` in ``CI``.
- ``merge_state`` is a ``CodeReview`` method: the adapter turns the mergeability of a PR, as
  ``open_prs`` reports it, into ``clean``, ``conflict`` or ``unknown``.
- ``GitHub.last_labeler`` is not in any port: the dispatcher no longer calls it.
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

    def issue(self, number):
        """Issue ``number`` as ``{number, title, body, labels, comments, state}``."""

    def issue_history(self, number):
        """Title, body and who changed them: ``{title, body, author, last_edited_at, editor,
        labeled, renamed}`` (see ``GitHub.issue_history``)."""

    def issue_comments(self, number):
        """The comments of an issue, oldest first, as ``{id, author: {login}, body}``."""

    def pr_comments(self, number):
        """The conversation comments of a pull request, as :meth:`issue_comments`."""

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
        """Post a comment; returns its id, or None if unknown."""

    def edit_comment(self, comment_id, body):
        """Replace the body of comment ``comment_id``."""

    def ensure_label(self, name, color, description):
        """Create or update label ``name``."""


@runtime_checkable
class CodeReview(Protocol):
    """Pull requests and their reviews."""

    def open_prs(self):
        """The open pull requests, with ``author`` in REST form (``author_login``)."""

    def merge_state(self, pr):
        """``clean``, ``conflict`` or ``unknown`` for ``pr`` as :meth:`open_prs` returns it."""

    def pr_head(self, number):
        """The current head commit of PR ``number``, read fresh."""

    def pr_reviews(self, number):
        """The native reviews of PR ``number``, oldest first, as ``{id, login, state}``."""

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
        """Every check of ``sha`` as ``{name, bucket, link, conclusion, run_id}``."""

    def check_runs(self, sha):
        """Every check run of ``sha``, superseded ones included, as ``{id, name, app_id, status,
        conclusion}``."""

    def failed_log(self, run_id, max_lines=80):
        """The last ``max_lines`` of the failed steps of run ``run_id``."""


@runtime_checkable
class Backend(Tracker, CodeReview, CI, Protocol):
    """The three ports together: what the dispatcher acts with, for itself and for each actor."""


PORTS = (Tracker, CodeReview, CI)


def port_methods(port):
    """The names of the methods ``port`` declares, in declaration order."""
    return [name for name, value in vars(port).items()
            if callable(value) and not name.startswith('_')]


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
