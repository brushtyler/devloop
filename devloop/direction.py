"""``devloop direction`` (#604): the operator's direction of the project.

ADR 0002, second precisazione of 2026-11-23, point 7: the operator writes the strategic direction
of a project on a dedicated issue, ``[direction] issue`` of the local overlay, and the
coordinator translates it into priorities. A direction is a comment of a user of
``trusted_users`` whose first line that is not blank is ``## Direzione``. Each one is complete and
replaces the one before: the latest counts, the others are history. Every other comment of the
issue (questions, proposals of the coordinator or of the supervisor) is discussion and changes
nothing; the comment of an app, devloop's included, never counts, even with the heading.

devloop does not interpret the content: it gives it back as it is, with its author, its dates and
its link. A comment of a trusted user that looks like a direction (a line ``Direzione`` as a
heading, or ``Direzione:``) without starting with the line ``## Direzione``, written after the
direction in force, is logged and not used. The direction never reaches the prompts of the
agents (implementer, reviewers, readiness check, conclave): only ``devloop direction`` and
``devloop status`` read it.
"""

import re
from dataclasses import dataclass

HEADING = '## Direzione'
# A line that looks like the heading of a direction: ``Direzione`` as a heading of any level,
# or followed by a colon, in any case, with or without emphasis.
_LOOKS_LIKE = re.compile(r'^\s*(?:#+\s*)?[*_]*\s*direzione\s*[*_]*\s*:?\s*[*_]*\s*$|'
                         r'^\s*[*_]*direzione[*_]*\s*:', re.IGNORECASE)


@dataclass(frozen=True)
class Direction:
    """The direction in force: the body of its comment as written, with its author, the time it
    was written and last changed (ISO 8601, UTC, as GitHub reports them) and its link."""
    issue: int
    body: str
    author: str
    created_at: str
    updated_at: str
    url: str

    def header(self):
        """One line: where the direction is, who wrote it and when."""
        edited = (f', edited {self.updated_at}'
                  if self.updated_at and self.updated_at != self.created_at else '')
        return (f'direction: issue #{self.issue}, comment of {self.author} at '
                f'{self.created_at}{edited}: {self.url}')


def is_app(login):
    """Whether ``login`` is a GitHub App's, as REST reports it (``<slug>[bot]``)."""
    return login.endswith('[bot]')


def starts_with_heading(body):
    """Whether the first line of ``body`` that is not blank is exactly ``## Direzione``
    (trailing spaces aside)."""
    for line in (body or '').splitlines():
        if line.strip():
            return line.rstrip() == HEADING
    return False


def looks_like(body):
    """Whether ``body`` has a line that looks like the heading of a direction."""
    return any(_LOOKS_LIKE.match(line) for line in (body or '').splitlines())


def _login(comment):
    author = comment.get('author') or {}
    return (author.get('login') if isinstance(author, dict) else None) or ''


def find(issue, comments, trusted_users, log=lambda line: None):
    """The ``Direction`` in force among ``comments`` of ``issue`` (oldest first, as
    ``Tracker.issue_comments`` returns them), or None when no comment is one. Only a comment of
    a login of ``trusted_users`` that is not an app's counts; the latest wins. The comments of
    trusted users after it that look like a direction without starting with ``## Direzione``
    are logged, never used."""
    trusted = frozenset(trusted_users)
    found = None
    doubtful = []
    for comment in comments:
        login = _login(comment)
        if not login or is_app(login) or login not in trusted:
            continue
        body = comment.get('body') or ''
        if starts_with_heading(body):
            found = comment
            doubtful = []
        elif looks_like(body):
            doubtful.append(comment)
    for comment in doubtful:
        log(f'direction: the comment of {_login(comment)} at {comment.get("created_at")} on '
            f'issue #{issue} ({comment.get("url")}) looks like a direction but does not start '
            f'with the line `{HEADING}`: not used')
    if found is None:
        return None
    return Direction(issue=issue, body=found.get('body') or '', author=_login(found),
                     created_at=found.get('created_at') or '',
                     updated_at=found.get('updated_at') or '', url=found.get('url') or '')


def read(cfg, gh, log=lambda line: None):
    """The direction in force of the project of ``cfg``, read with ``gh``, or None without
    ``[direction] issue`` or without a valid comment. Raises what ``gh`` raises."""
    issue = cfg.direction.issue
    if not issue:
        return None
    return find(issue, gh.issue_comments(issue), cfg.trusted_users, log=log)


def missing(cfg):
    """Why there is no direction in force, for a ``read`` that returned None."""
    if not cfg.direction.issue:
        return ('direction: none: no [direction] issue is configured; the priorities are those '
                'of the operator\'s labels')
    return (f'direction: none: no comment of a trusted user on issue #{cfg.direction.issue} '
            f'starts with the line `{HEADING}`; the priorities are those of the operator\'s '
            'labels')
