"""Thin wrapper over the `gh` CLI. All GitHub state devloop relies on goes through here.

Checks, labels, comments and pull request creation use the REST API through `gh api`, which
behaves the same across `gh` versions. Porcelain commands are used only for reads whose `--json`
fields are long-standing (`issue list/view`, `pr list/view`); a field newer `gh` added (such as
an issue's ``stateReason``) is read with REST instead.

``GitHub`` is the concrete adapter of the ports of ``devloop.ports`` (``Tracker``,
``CodeReview`` and ``CI``); the dispatcher depends on the ports, not on this class.
"""

import json
import math
import os
import re
import subprocess
import threading
from datetime import datetime
from urllib.parse import quote

from .ports import summarize_checks

_RUN_ID = re.compile(r'/actions/runs/(\d+)')
_JOB_ID = re.compile(r'/actions/runs/\d+/job/(\d+)')
# The text of an issue and its ``labeled`` and ``renamed`` events, read forwards one page at a
# time from ``$after`` (#483: every page, not only the latest 100 events).
_ISSUE_HISTORY = (
    'query($owner:String!,$name:String!,$number:Int!,$after:String){'
    'repository(owner:$owner,name:$name){'
    'issue(number:$number){title body author{__typename login} lastEditedAt '
    'editor{__typename login} '
    'timelineItems(itemTypes:[LABELED_EVENT,RENAMED_TITLE_EVENT],first:100,after:$after){'
    'pageInfo{hasNextPage endCursor} nodes{__typename '
    '... on LabeledEvent{createdAt actor{__typename login} label{name}} '
    '... on RenamedTitleEvent{createdAt actor{__typename login}}}}}}}')
# The most pages ``GitHub.issue_history`` reads (#483): far more than any issue has. Past them
# the read is incomplete.
_HISTORY_PAGES = 100
# Native dependencies of an issue (#277): the issues that block it, with their state (GitHub
# allows at most 50 per issue), and who added each "blocked by" (the timeline events, read
# backwards one page at a time, from ``$before``).
_ISSUE_DEPENDENCIES = (
    'query($owner:String!,$name:String!,$number:Int!,$before:String){'
    'repository(owner:$owner,name:$name){'
    'issue(number:$number){author{__typename login} '
    'blockedBy(first:50){nodes{number state repository{nameWithOwner}}} '
    'timelineItems(itemTypes:[BLOCKED_BY_ADDED_EVENT],last:100,before:$before){'
    'pageInfo{hasPreviousPage startCursor} nodes{__typename '
    '... on BlockedByAddedEvent{createdAt actor{__typename login} '
    'blockingIssue{number repository{nameWithOwner}}}}}}}}')
_FAILED_CONCLUSIONS = {'failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure',
                       'stale'}
_FAILED_STATUSES = {'failure', 'error'}
# What `gh` reports for an issue or PR that does not exist (deleted, transferred): GraphQL for
# `gh pr view` and `gh issue view`, the HTTP status for `gh api`.
_NOT_FOUND = re.compile(r'Could not resolve to an? (?:PullRequest|issue)\b|\(HTTP (?:404|410)\)')
# A failed write whose outcome is unknown (#364): GitHub answered 5xx, or the connection failed
# without proof that the request was never sent. An answer 4xx, and a failure before the
# connection opened (`dial tcp`: a name that does not resolve, a refused connection; gh's
# `error connecting to` for a name that does not resolve; a failed TLS handshake), are certain:
# nothing was written.
_HTTP_STATUS = re.compile(r'\bHTTP (\d{3})\b')
_NOT_SENT = re.compile(r'\bdial tcp\b|\berror connecting to\b|\bTLS handshake\b')
# The exit codes of coreutils ``timeout`` once it stopped its command (#397): 124 after SIGTERM,
# 137 (128 + 9) after SIGKILL.
_TIMED_OUT = (124, 137)
# The temporary directory of devloop's `gh` (``GitHub.tmp_dir``, #545), in ``state_dir``.
GH_TMP_DIR = 'gh-tmp'


class GitHubError(RuntimeError):
    pass


class GitHubNotFound(GitHubError):
    """GitHub answered that the object asked for does not exist: an explicit result, unlike a
    failed call."""


class GitHubUncertain(GitHubError):
    """A ``comment``, ``edit_comment``, ``submit_review`` or ``create_pr`` whose outcome is
    unknown (#364, ADR 0001 "Porta del tracker"; the last two since #385, only for the outbox of
    the publications, ``GitHub.uncertain_publications``): a timeout, a
    connection lost after sending, a 5xx answer. Unlike any other ``GitHubError``, the write may
    have taken effect: ``ports.reconcile_comment`` tells whether a comment did, the outbox of
    the publications (``outbox.Outbox``) whether any of them did. Being a ``GitHubError``, it is
    handled as a failed write wherever nothing reconciles it."""


class LogUnreadable(GitHubError):
    """`gh` ran and could not read the failed log asked for (#545): expired, not yet available,
    a cache it cannot write. The message holds its stderr, cut. Unlike a timeout or no time
    left, it is an answer: the caller goes on without the log, and logs why."""


def _uncertain(stderr):
    """Whether a write that `gh` reported as failed with ``stderr`` may still have taken
    effect: an answer 5xx, or a transport failure not known to precede the request."""
    status = _HTTP_STATUS.search(stderr)
    if status:
        return int(status.group(1)) >= 500
    return not _NOT_SENT.search(stderr)


def _login(who):
    return (who or {}).get('login')


def graphql_login(actor):
    """A GraphQL actor's login in REST form. GraphQL reports a GitHub App (``Bot``) as a bare
    ``<slug>``, indistinguishable from a user; REST and the trust checks use ``<slug>[bot]``."""
    login = _login(actor)
    if login and (actor or {}).get('__typename') == 'Bot' and not login.endswith('[bot]'):
        return login + '[bot]'
    return login


def author_login(author):
    """A login in REST form. `gh` porcelain reports an app as ``app/<slug>``; REST and the
    trust checks use ``<slug>[bot]``."""
    login = _login(author)
    if login and (author or {}).get('is_bot') and login.startswith('app/'):
        return login[len('app/'):] + '[bot]'
    return login


class GitHub:
    """The adapter of ``ports.Tracker``, ``ports.CodeReview`` and ``ports.CI`` on GitHub.

    ``token``, when given, is a callable returning the token every call runs with; otherwise
    the ambient ``gh`` login is used. ``time_left``, when given, is a callable returning the
    seconds the calls may still take (None: no limit): each call gets at most that long, and
    none starts once it is spent. It belongs to the thread that sets it: the wait of ``devloop
    run`` bounds its own reads, not those of the actions running meanwhile in other threads on
    the same client (#227)."""

    timeout = 300  # seconds, for each call
    # A write whose `gh` holds the dispatcher lock (``lock_fd``, #397) runs under ``timeout``
    # (coreutils), a limit of its own that does not die with the dispatcher: SIGTERM once the
    # call's timeout is spent, SIGKILL this many seconds later. The call's own timeout, applied
    # by this process, comes ``WRAPPED_GRACE`` seconds after that: killing ``timeout`` first
    # would leave its `gh` running, and holding the lock, with no limit.
    KILL_AFTER = 5
    WRAPPED_GRACE = KILL_AFTER + 10

    def __init__(self, repo, run=subprocess.run, token=None, time_left=None, tmp_dir=None):
        self.repo = repo
        self._run = run
        self._token = token
        # The temporary directory of each `gh` (``TMPDIR``, #545), created with mode 700 at the
        # first call, or None for the system's: `gh` keeps there the cache of the logs it
        # downloads (``gh-cli-cache``), which in a ``/tmp`` shared with other users may be
        # theirs, and not writable.
        self.tmp_dir = tmp_dir
        self._local = threading.local()
        self.time_left = time_left
        # The descriptor of the dispatcher lock, set by devloop once it holds it, only with the
        # outbox of the publications on (``apps.Identities.hold``, #385, #397). The `gh` of each
        # write gets it open: no other dispatcher takes the lock while a `gh` of this one runs,
        # even one that outlived it, so the outbox (``outbox.Outbox``) knows that the attempts
        # a stop cut short had ended when the next dispatcher started. That `gh` runs under a
        # timeout of its own (``KILL_AFTER``): a `gh` stuck on the network never holds the lock
        # for ever.
        self.lock_fd = None
        # The descriptor of the repository lock (ADR 0006, R8), set with ``lock_fd`` when
        # devloop holds that lock too: the `gh` of each write keeps it open as well, so that no
        # dispatcher with another state_dir takes over the repository while a publication of
        # this one may still be running.
        self.repo_lock_fd = None
        # Whether a ``submit_review`` or ``create_pr`` whose outcome is unknown raises
        # ``GitHubUncertain`` (#385): only for the outbox of the publications, which reconciles
        # it; without it they fail as any failed write, as before it (#397, ADR 0004, D8).
        self.uncertain_publications = False

    @property
    def time_left(self):
        return getattr(self._local, 'time_left', None)

    @time_left.setter
    def time_left(self, value):
        self._local.time_left = value

    def _gh(self, *args, stdin=None, check=True, write=False):
        """Run `gh` with ``args``. ``write``: the call is a ``comment``, ``edit_comment``,
        ``submit_review`` or ``create_pr``, whose timeout or uncertain failure raises
        ``GitHubUncertain`` (#364, #385), and whose `gh`, with ``lock_fd`` set, holds the
        dispatcher lock open and runs under ``timeout`` (#397)."""
        kwargs = {}
        self._time_left(args)  # no token is minted once the time is spent
        if self._token is not None:
            env = {k: v for k, v in os.environ.items() if k not in ('GITHUB_TOKEN', 'GH_TOKEN')}
            kwargs['env'] = dict(env, GH_TOKEN=self._token())
        if self.tmp_dir is not None:
            kwargs['env'] = dict(kwargs.get('env', os.environ), TMPDIR=self._tmp(args))
        held = write and self.lock_fd is not None
        if held:
            kwargs['pass_fds'] = tuple(fd for fd in (self.lock_fd, self.repo_lock_fd)
                                       if fd is not None)
        timeout = self.timeout
        left = self._time_left(args)  # what the mint of the token left
        if left is not None:
            timeout = min(timeout, left)
        command = ['gh', *args]
        if held:
            command = ['timeout', '-k', str(self.KILL_AFTER), str(max(1, math.ceil(timeout))),
                       *command]
        try:
            done = self._run(command, input=stdin, capture_output=True, text=True,
                             timeout=timeout + self.WRAPPED_GRACE if held else timeout,
                             **kwargs)
        except subprocess.TimeoutExpired as exc:
            error = GitHubUncertain if write else GitHubError
            raise error(f'gh {" ".join(args[:3])}: timed out after {exc.timeout}s') from exc
        except OSError as exc:
            raise GitHubError(f'{command[0]} could not start: {exc}') from exc
        if held and (done.returncode in _TIMED_OUT or done.returncode < 0):
            # Stopped by its own timeout (124, 137 after SIGKILL), or killed: never an answer.
            raise GitHubUncertain(f'gh {" ".join(args[:3])}: timed out after '
                                  f'{max(1, math.ceil(timeout))}s (exit {done.returncode})')
        if check and done.returncode != 0:
            stderr = done.stderr or ''
            if _NOT_FOUND.search(stderr):
                error = GitHubNotFound
            elif write and _uncertain(stderr):
                error = GitHubUncertain
            else:
                error = GitHubError
            raise error(f'gh {" ".join(args[:3])}: {stderr.strip()[:500]}')
        return done

    def _tmp(self, args):
        """``tmp_dir``, created if missing, with mode 700 (#545)."""
        tmp = os.fspath(self.tmp_dir)
        try:
            os.makedirs(tmp, mode=0o700, exist_ok=True)
            os.chmod(tmp, 0o700)
        except OSError as exc:
            raise GitHubError(f'gh {" ".join(args[:3])}: temporary directory {tmp}: '
                              f'{exc}') from exc
        return tmp

    def _time_left(self, args):
        left = self.time_left() if self.time_left is not None else None
        if left is not None and left <= 0:
            raise GitHubError(f'gh {" ".join(args[:3])}: no time left')
        return left

    def _json(self, *args):
        return json.loads(self._gh(*args).stdout or 'null')

    def _json_lines(self, *args):
        return [json.loads(line) for line in self._gh(*args).stdout.splitlines() if line.strip()]

    # Reads ---------------------------------------------------------------
    def issues_with_label(self, label):
        return self._json('issue', 'list', '-R', self.repo, '--state', 'open', '--label', label,
                          '--limit', '200', '--json', 'number,title,labels,author')

    def issue(self, number):
        return self._json('issue', 'view', str(number), '-R', self.repo,
                          '--json', 'number,title,body,labels,comments,state')

    def open_issues(self, limit=300):
        """Every open issue (pull requests excluded), up to ``limit``, as ``gh issue list``
        reports it: ``{number, title, author, labels}``, with the author as ``author_login``
        reads it."""
        return self._json('issue', 'list', '-R', self.repo, '--state', 'open', '--limit',
                          str(limit), '--json', 'number,title,author,labels')

    def open_issue_numbers(self):
        """The numbers of every open issue, pull requests excluded, read with REST page by page
        until the last, without a limit on their count (#656)."""
        return self._json_lines('api', '--paginate',
                                f'repos/{self.repo}/issues?state=open&per_page=100',
                                '--jq', '.[] | select(.pull_request == null) | .number')

    def issue_state(self, number):
        """``{state, stateReason}`` of issue ``number``, read fresh: ``state`` OPEN or CLOSED,
        ``stateReason`` (COMPLETED, NOT_PLANNED, ...) or None, both in upper case as `gh issue
        view` reports them. Read with REST: older `gh` (2.46 among them) has no ``stateReason``
        field in `gh issue view --json` (#211)."""
        raw = self._json('api', f'repos/{self.repo}/issues/{int(number)}',
                         '--jq', '{state, state_reason}') or {}
        return {'state': (raw.get('state') or '').upper() or None,
                'stateReason': (raw.get('state_reason') or '').upper() or None}

    def issue_author(self, number):
        """``{login, pull_request}`` of issue or PR ``number``, whatever its state: its author's
        login in REST form and whether it is a PR."""
        return self._json('api', f'repos/{self.repo}/issues/{int(number)}', '--jq',
                          '{login: .user.login, pull_request: (.pull_request != null)}')

    def issue_labels(self, number):
        """The names of the labels of issue or PR ``number``, whatever its state, read fresh
        (#676)."""
        return self._json('api', f'repos/{self.repo}/issues/{int(number)}',
                          '--jq', '[.labels[].name]') or []

    def comments_since(self, since):
        """Conversation comments of every issue and PR created or edited at ``since`` (ISO 8601,
        UTC) or later, as ``{id, login, number, body, pull_request}``, ``pull_request`` True
        for a comment of a PR (#656). Read with REST, so an app's login is ``<slug>[bot]``."""
        return self._json_lines('api', '--paginate',
                                f'repos/{self.repo}/issues/comments?since={since}&per_page=100',
                                '--jq', '.[] | {id, login: .user.login, body, '
                                        'number: (.issue_url | split("/") | last | tonumber), '
                                        'pull_request: (.html_url | test("/pull/"))}')

    def dated_comments(self, since=None, number=None):
        """Conversation comments with their creation time (#256), oldest first, as ``{id,
        login, number, body, created_at, url}``: of issue or PR ``number``, or of every issue
        and PR without it; with ``since`` (ISO 8601, UTC) only those created or edited at that
        time or later. Read with REST, so an app's login is ``<slug>[bot]``."""
        path = (f'repos/{self.repo}/issues/{int(number)}/comments' if number is not None
                else f'repos/{self.repo}/issues/comments')
        query = '?per_page=100' + (f'&since={since}' if since else '')
        return self._json_lines('api', '--paginate', path + query,
                                '--jq', '.[] | {id, login: .user.login, body, created_at, '
                                        'url: .html_url, '
                                        'number: (.issue_url | split("/") | last | tonumber)}')

    def search_prs(self, query):
        """The PRs, in any state, that the GitHub search ``query`` finds (#256), as ``{number,
        body, author, createdAt, mergedAt}``, with the author as ``author_login`` reads it."""
        prs = self._json('pr', 'list', '-R', self.repo, '--state', 'all', '--search', query,
                         '--limit', '500', '--json', 'number,body,author,createdAt,mergedAt')
        return [dict(raw, author={'login': author_login(raw.get('author'))}) for raw in prs]

    def edit_issue_body(self, number, body):
        """Replace the body of issue ``number`` (#256: the report issue)."""
        self._gh('api', '-X', 'PATCH', f'repos/{self.repo}/issues/{int(number)}',
                 '-F', 'body=@-', stdin=body)

    def issue_history(self, number):
        """Title, body and who changed them, with their history: ``{title, body, author,
        last_edited_at, editor, labeled, renamed, complete}``. ``labeled`` is a list of
        ``(label, login, at)`` and ``renamed`` of ``(login, at)``, oldest first. Logins are in
        REST form (``graphql_login``): an app is ``<slug>[bot]``.

        The events are read forwards, 100 at a time, up to the last page (#483). The text is
        the one of the last read, so it is never older than the events read before it.
        ``complete`` is True only when the last page read says there is no other. A page that
        says nothing about the next one, one that says there is another without its cursor, or
        more than ``_HISTORY_PAGES`` pages leave it False: what needs every event
        (``context.issue_decisions``) then admits nothing."""
        owner, name = self.repo.split('/', 1)
        nodes, after, complete = [], None, False
        for _ in range(_HISTORY_PAGES):
            cursor = ['-f', f'after={after}'] if after else []
            data = self._json('api', 'graphql', '-f', f'query={_ISSUE_HISTORY}',
                              '-f', f'owner={owner}', '-f', f'name={name}',
                              '-F', f'number={int(number)}', *cursor)
            issue = data['data']['repository']['issue']
            timeline = issue['timelineItems']
            nodes.extend(n for n in timeline['nodes'] if n)
            info = timeline.get('pageInfo') or {}
            if info.get('hasNextPage') is False:
                complete = True
                break
            after = info.get('endCursor')
            if info.get('hasNextPage') is not True or not after:
                break
        return {
            'title': issue['title'], 'body': issue['body'] or '',
            'author': graphql_login(issue['author']), 'last_edited_at': issue['lastEditedAt'],
            'editor': graphql_login(issue['editor']),
            'labeled': [((n.get('label') or {}).get('name'), graphql_login(n['actor']),
                         n['createdAt'])
                        for n in nodes if n['__typename'] == 'LabeledEvent'],
            'renamed': [(graphql_login(n['actor']), n['createdAt'])
                        for n in nodes if n['__typename'] == 'RenamedTitleEvent'],
            'complete': complete,
        }

    def issue_dependencies(self, number):
        """The native "blocked by" dependencies of issue ``number`` (#277): ``{author,
        blocked_by, added}``. ``blocked_by`` lists the issues that block it now as ``{repo,
        number, state}`` (``state`` OPEN or CLOSED, ``repo`` as ``owner/name``); ``added`` the
        events that added a blocker, oldest first, as ``{repo, number, by, at}``. Logins are in
        REST form (``graphql_login``).

        The events are read backwards, 100 at a time, until every open blocker has its latest
        addition or the timeline is over: an open block missing from ``added`` has no event at
        all, it is never one cut off by a truncated read. ``author`` and ``blocked_by`` are those
        of the first read."""
        owner, name = self.repo.split('/', 1)

        def where(node):
            return (node.get('repository') or {}).get('nameWithOwner'), node.get('number')

        issue, pages, before = None, [], None
        while True:
            cursor = ['-f', f'before={before}'] if before else []
            data = self._json('api', 'graphql', '-f', f'query={_ISSUE_DEPENDENCIES}',
                              '-f', f'owner={owner}', '-f', f'name={name}',
                              '-F', f'number={int(number)}', *cursor)
            read = data['data']['repository']['issue']
            issue = issue or read
            timeline = read['timelineItems']
            pages.append([n for n in timeline['nodes']
                          if n.get('__typename') == 'BlockedByAddedEvent'
                          and n.get('blockingIssue')])
            found = {where(n['blockingIssue']) for page in pages for n in page}
            missing = {where(n) for n in issue['blockedBy']['nodes']
                       if n.get('state') == 'OPEN'} - found
            info = timeline.get('pageInfo') or {}
            before = info.get('startCursor')
            if not missing or not info.get('hasPreviousPage') or not before:
                break
        blocked_by = []
        for node in issue['blockedBy']['nodes']:
            repo, blocker = where(node)
            blocked_by.append({'repo': repo, 'number': blocker, 'state': node.get('state')})
        added = []
        for page in reversed(pages):  # the oldest page was read last
            for node in page:
                repo, blocker = where(node['blockingIssue'])
                added.append({'repo': repo, 'number': blocker,
                              'by': graphql_login(node.get('actor')),
                              'at': node.get('createdAt')})
        return {'author': graphql_login(issue['author']), 'blocked_by': blocked_by,
                'added': added}

    def labelers(self, number, label):
        """Logins of every ``labeled`` event of ``label`` on issue or PR ``number``, oldest
        first, including events of a label removed since."""
        done = self._gh('api', '--paginate', f'repos/{self.repo}/issues/{number}/events',
                        '--jq', f'.[] | select(.event == "labeled" and .label.name == {json.dumps(label)})'
                                ' | .actor.login')
        return done.stdout.split()

    def label_events(self, number, label):
        """The ``labeled`` and ``unlabeled`` events of ``label`` on issue or PR ``number``,
        oldest first, as ``{event, by, at}``."""
        select = (f'select((.event == "labeled" or .event == "unlabeled") '
                  f'and .label.name == {json.dumps(label)})')
        return self._json_lines('api', '--paginate', f'repos/{self.repo}/issues/{number}/events',
                                '--jq', f'.[] | {select} | '
                                        '{event, by: .actor.login, at: .created_at}')

    def labeled_events(self, number):
        """Every ``labeled`` event on issue or PR ``number``, oldest first, as ``{label, by,
        at}``, including events of labels removed since."""
        return self._json_lines('api', '--paginate', f'repos/{self.repo}/issues/{number}/events',
                                '--jq', '.[] | select(.event == "labeled") | '
                                        '{label: .label.name, by: .actor.login, at: .created_at}')

    def last_labeler(self, number, label):
        """Login of whoever most recently added ``label`` to issue ``number``."""
        logins = self.labelers(number, label)
        return logins[-1] if logins else None

    def open_prs(self):
        prs = self._json('pr', 'list', '-R', self.repo, '--state', 'open', '--limit', '200',
                         '--json', 'number,title,body,labels,headRefName,headRefOid,author,'
                                   'isCrossRepository,mergeable,mergeStateStatus')
        return [dict(raw, author={'login': author_login(raw.get('author'))}) for raw in prs]

    def merged_prs(self, branch):
        """The merged PRs whose head branch is ``branch``, newest first, with the fields of
        :meth:`open_prs` (the mergeability ones aside) and ``mergeCommit`` (#228)."""
        prs = self._json('pr', 'list', '-R', self.repo, '--state', 'merged', '--head', branch,
                         '--limit', '20', '--json', 'number,title,body,labels,headRefName,'
                                                    'headRefOid,author,isCrossRepository,'
                                                    'mergeCommit')
        return [dict(raw, author={'login': author_login(raw.get('author'))}) for raw in prs]

    def commit_prs(self, sha):
        """The numbers of the merged PRs that GitHub associates with commit ``sha`` (#473): for
        a commit of the base branch, the PR whose merge (a merge commit, a squash or the last
        commit of a rebase) brought it there."""
        prs = self._json('api', f'repos/{self.repo}/commits/{sha}/pulls') or []
        return [raw['number'] for raw in prs
                if isinstance(raw, dict) and raw.get('merged_at')
                and isinstance(raw.get('number'), int)]

    def branch_prs(self, branch):
        """The PRs whose head branch is ``branch``, in any state, newest first, with the fields
        of :meth:`open_prs` (the mergeability ones aside), ``state`` and ``createdAt`` (#384)."""
        prs = self._json('pr', 'list', '-R', self.repo, '--state', 'all', '--head', branch,
                         '--limit', '20', '--json', 'number,title,body,labels,headRefName,'
                                                    'headRefOid,author,isCrossRepository,'
                                                    'state,createdAt')
        return [dict(raw, author={'login': author_login(raw.get('author'))}) for raw in prs]

    def find_pr(self, number):
        """PR ``number`` in any state, with the fields of :meth:`open_prs` (the mergeability
        ones aside), ``state`` and ``mergeCommit`` (``{oid}`` once merged, #473), or None when
        ``number`` is an issue (#362). Read with REST first, so that an issue is told from a PR
        without reading an error."""
        kind = self._json('api', f'repos/{self.repo}/issues/{int(number)}',
                          '--jq', '{pull_request: (.pull_request != null)}') or {}
        if not kind.get('pull_request'):
            return None
        raw = self._json('pr', 'view', str(int(number)), '-R', self.repo, '--json',
                         'number,title,body,labels,headRefName,headRefOid,author,'
                         'isCrossRepository,state,mergeCommit')
        return dict(raw, author={'login': author_login(raw.get('author'))})

    def merge_state(self, pr):
        """``clean``, ``conflict`` or ``unknown`` for ``pr`` as :meth:`open_prs` returns it."""
        return merge_state(pr.get('mergeable'), pr.get('mergeStateStatus'))

    def pr_comments(self, number):
        """Conversation comments, read with REST so that an app's login is ``<slug>[bot]``
        (`gh pr view` reports it as a bare ``<slug>``, indistinguishable from a user), with the
        time each was written (``created_at``) and last changed (``updated_at``, #472), ISO
        8601, UTC, and its permalink (``url``, ``html_url``, #506)."""
        return self._json_lines('api', '--paginate', f'repos/{self.repo}/issues/{number}/comments',
                                '--jq', '.[] | {id, author: {login: .user.login}, body, '
                                        'created_at, updated_at, url: .html_url}')

    def issue_comments(self, number):
        """An issue's comments, oldest first, read with REST as :meth:`pr_comments`."""
        return self.pr_comments(number)

    def pr_head(self, number):
        """The PR's current head commit, read fresh."""
        return self._json('api', f'repos/{self.repo}/pulls/{number}')['head']['sha']

    def pr_state(self, number):
        """The PR as a merge needs it, read fresh: ``{state, headRefOid, mergeable,
        mergeStateStatus, labels, mergeCommit}`` (``mergeCommit``: ``{oid}`` once merged)."""
        return self._json('pr', 'view', str(number), '-R', self.repo, '--json',
                          'state,headRefOid,mergeable,mergeStateStatus,labels,mergeCommit')

    def checks(self, sha):
        """``(state, failed_names, failed_run_ids)`` for commit ``sha``.

        ``state`` is pass, fail, pending or none. Both check runs (Actions) and commit statuses
        count.
        """
        return summarize_checks(self.check_details(sha))

    def check_details(self, sha):
        """Every check of commit ``sha`` as ``normalize_checks`` returns it: check runs and
        commit statuses, each with its outcome, its duration and, for a GitHub Actions run, its
        run id. Of the check runs with the same name and app (a re-run in another suite) only
        the most recent counts."""
        runs = self._json_lines('api', '--paginate', f'repos/{self.repo}/commits/{sha}/check-runs',
                                '--jq', '.check_runs[] | {id, name, app_id: .app.id, started_at, '
                                        'completed_at, status, conclusion, details_url}')
        combined = self._json('api', f'repos/{self.repo}/commits/{sha}/status?per_page=100')
        return normalize_checks(runs, combined or {})

    def check_runs(self, sha):
        """Every check run of commit ``sha``, superseded runs included, as ``{id, name, app_id,
        status, conclusion}``. Commit statuses are not check runs and are not listed."""
        return self._json_lines('api', '--paginate',
                                f'repos/{self.repo}/commits/{sha}/check-runs?filter=all'
                                '&per_page=100',
                                '--jq', '.check_runs[] | {id, name, app_id: .app.id, status, '
                                        'conclusion}')

    def workflow_runs(self, sha):
        """The GitHub Actions workflow runs of commit ``sha``, as ``{id, name, status,
        conclusion, run_attempt}`` (#615). A re-run of failed jobs makes its run ``queued``
        again at once, before its new jobs show among the check runs."""
        return self._json_lines('api', '--paginate',
                                f'repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100',
                                '--jq', '.workflow_runs[] | {id, name, status, conclusion, '
                                        'run_attempt}')

    def pr_reviews(self, number):
        """The native reviews of PR ``number``, oldest first, as ``{id, login, state, body,
        submitted_at}`` (state: APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING;
        ``submitted_at``, ISO 8601 in UTC, is null for a pending review). The body is the
        record of a devloop review written by an agent with its own app (#325)."""
        return self._json_lines('api', '--paginate',
                                f'repos/{self.repo}/pulls/{number}/reviews?per_page=100',
                                '--jq', '.[] | {id, login: .user.login, state, body, '
                                        'submitted_at}')

    def failed_log(self, run_id, max_lines=80, job_id=None):
        """The last ``max_lines`` of the failed steps of run ``run_id`` (all of them with None),
        or only of its job ``job_id`` when given (#200). When `gh` cannot read the log it raises
        ``LogUnreadable``, with its stderr (#545)."""
        target = ('--job', str(int(job_id))) if job_id is not None else (str(run_id),)
        done = self._gh('run', 'view', *target, '-R', self.repo, '--log-failed', check=False)
        if done.returncode != 0:
            reason = ' '.join((done.stderr or '').split())[:300] or f'exit {done.returncode}'
            raise LogUnreadable(f'gh run view {" ".join(target)}: {reason}')
        lines = done.stdout.splitlines()
        return '\n'.join(lines if max_lines is None else lines[-max_lines:])

    def job_log(self, job_id):
        """The whole log of GitHub Actions job ``job_id`` (#300), or None when `gh` cannot read
        it (expired, deleted, not yet available)."""
        done = self._gh('run', 'view', '--job', str(int(job_id)), '-R', self.repo, '--log',
                        check=False)
        return done.stdout if done.returncode == 0 else None

    def rerun_failed_jobs(self, run_id):
        """Re-run the failed jobs of GitHub Actions run ``run_id`` (#200). It needs ``actions:
        write``: without it GitHub answers 403 and this raises ``GitHubError``."""
        self._gh('api', '-X', 'POST',
                 f'repos/{self.repo}/actions/runs/{int(run_id)}/rerun-failed-jobs')

    def commit_date(self, sha):
        """The committer date of commit ``sha``, as GitHub reports it (#263), or None."""
        found = self._json('api', f'repos/{self.repo}/commits/{sha}',
                           '--jq', '{date: .commit.committer.date}')
        return (found or {}).get('date')

    def required_checks(self, branch):
        """The names of the checks required on ``branch`` (#263): those of the
        ``required_status_checks`` rules of the rulesets that apply to it, and the contexts of
        its classic branch protection, sorted and without repetitions."""
        name = quote(branch)
        rules = self._json_lines('api', '--paginate',
                                 f'repos/{self.repo}/rules/branches/{name}?per_page=100',
                                 '--jq', '.[] | select(.type == "required_status_checks") '
                                         '| .parameters.required_status_checks[] | {context}')
        found = {rule['context'] for rule in rules if rule.get('context')}
        protection = self._json('api', f'repos/{self.repo}/branches/{name}',
                                '--jq', '{contexts: .protection.required_status_checks.contexts}')
        found.update(c for c in (protection or {}).get('contexts') or () if c)
        return sorted(found)

    # Writes --------------------------------------------------------------
    def edit_labels(self, number, add=(), remove=()):
        if add:
            args = ['api', '-X', 'POST', f'repos/{self.repo}/issues/{number}/labels']
            for label in add:
                args += ['-f', f'labels[]={label}']
            self._gh(*args)
        for label in remove:
            done = self._gh('api', '-X', 'DELETE',
                            f'repos/{self.repo}/issues/{number}/labels/{quote(label, safe="")}',
                            check=False)
            # Removing a label that is not there is a no-op, not an error.
            if done.returncode != 0 and 'Label does not exist' not in done.stdout + done.stderr:
                raise GitHubError(f'remove label {label} from #{number}: {done.stderr.strip()[:500]}')

    def comment(self, number, body):
        """Post a comment; returns its id, or None if the response does not carry one. Raises
        ``GitHubUncertain`` when the outcome is unknown (#364), any other ``GitHubError`` when
        the comment was certainly not posted."""
        done = self._gh('api', '-X', 'POST', f'repos/{self.repo}/issues/{number}/comments',
                        '-F', 'body=@-', stdin=body, write=True)
        try:
            created = json.loads(done.stdout or 'null')
        except json.JSONDecodeError:
            return None
        return created.get('id') if isinstance(created, dict) else None

    def close_issue(self, number):
        """Close issue ``number`` as completed (#228)."""
        self._gh('api', '-X', 'PATCH', f'repos/{self.repo}/issues/{int(number)}',
                 '-f', 'state=closed', '-f', 'state_reason=completed')

    def edit_comment(self, comment_id, body):
        """Replace the body of comment ``comment_id`` of an issue or a PR. Raises
        ``GitHubUncertain`` when the outcome is unknown (#364), any other ``GitHubError`` when
        the body was certainly not replaced."""
        self._gh('api', '-X', 'PATCH', f'repos/{self.repo}/issues/comments/{int(comment_id)}',
                 '-F', 'body=@-', stdin=body, write=True)

    def create_pr(self, *, base, head, title, body):
        """``(number, url)`` of the new PR. Labels are the dispatcher's job, not the author's.
        With ``uncertain_publications`` (the outbox of the publications, #385, #397) it raises
        ``GitHubUncertain`` when the outcome is unknown, any other ``GitHubError`` when the PR
        was certainly not opened; without it, any failure is a ``GitHubError``."""
        created = json.loads(self._gh(
            'api', '-X', 'POST', f'repos/{self.repo}/pulls', '-f', f'title={title}',
            '-f', f'head={head}', '-f', f'base={base}', '-F', 'body=@-', stdin=body,
            write=self.uncertain_publications).stdout)
        return created['number'], created['html_url']

    def submit_review(self, number, *, commit, event, body):
        """A native PR review (APPROVE, REQUEST_CHANGES or COMMENT) on ``commit``. With
        ``uncertain_publications`` (#385, #397) it raises ``GitHubUncertain`` when the outcome
        is unknown, any other ``GitHubError`` when the review was certainly not submitted;
        without it, any failure is a ``GitHubError``."""
        self._gh('api', '-X', 'POST', f'repos/{self.repo}/pulls/{number}/reviews',
                 '-f', f'commit_id={commit}', '-f', f'event={event}', '-F', 'body=@-', stdin=body,
                 write=self.uncertain_publications)

    def merge_pr(self, number, head):
        """Merge PR ``number`` with a merge commit, only if its head is still ``head``. The
        branch is kept."""
        self._gh('pr', 'merge', str(number), '-R', self.repo, '--merge',
                 '--match-head-commit', head)

    def ensure_label(self, name, color, description):
        self._gh('label', 'create', name, '-R', self.repo, '--color', color,
                 '--description', description, '--force')

    def repo_labels(self):
        """The names of the repository's labels."""
        labels = self._json('label', 'list', '-R', self.repo, '--limit', '1000', '--json', 'name')
        return [label['name'] for label in labels or []]

    def rename_label(self, name, new_name, color, description):
        """Rename label ``name`` to ``new_name`` (#248): an edit, not a new label, so that the
        issues and pull requests that have it keep it."""
        self._gh('label', 'edit', name, '-R', self.repo, '--name', new_name, '--color', color,
                 '--description', description)


def merge_state(mergeable, merge_state_status):
    """``conflict``, ``unknown`` or ``clean`` from a PR's ``mergeable`` and ``mergeStateStatus``.

    A conflict is ``mergeable`` CONFLICTING (``false`` in REST) or the state DIRTY. While GitHub
    is still computing (UNKNOWN, or a missing value) the state is ``unknown``, not a conflict.
    Other states (BLOCKED, BEHIND, UNSTABLE, ...) concern checks and rules, not conflicts.
    """
    if mergeable in ('CONFLICTING', False) or merge_state_status == 'DIRTY':
        return 'conflict'
    if mergeable in ('UNKNOWN', None) or merge_state_status in ('UNKNOWN', None):
        return 'unknown'
    return 'clean'


def _status_bucket(state):
    return 'fail' if state in _FAILED_STATUSES else 'pending' if state == 'pending' else 'pass'


def latest_check_runs(check_runs):
    """Only the most recent of the check runs with the same name and app: the one with the
    greatest ``started_at`` and, on a tie, the greatest ``id``. ``commits/<sha>/check-runs``
    returns the latest run of each suite, so a re-run in a new suite leaves the superseded run
    there too, possibly stuck ``in_progress``. The kept runs stay in their original order."""
    latest = {}
    for index, run in enumerate(check_runs):
        key = (run.get('name'), run.get('app_id'))
        rank = (run.get('started_at') or '', run.get('id') or 0)
        if key not in latest or rank >= latest[key][0]:
            latest[key] = (rank, index)
    kept = sorted(index for _, index in latest.values())
    return [check_runs[index] for index in kept]


def _instant(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        return None


def run_seconds(run):
    """The whole seconds a completed check run took, from its ``started_at`` to its
    ``completed_at`` (#300), or None: still running, or a time missing or unreadable."""
    if run.get('status') != 'completed':
        return None
    start, end = _instant(run.get('started_at')), _instant(run.get('completed_at'))
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def job_id(link):
    """The GitHub Actions job of a check run, read from its link
    (``…/actions/runs/<run>/job/<job>``), or None (#200)."""
    found = _JOB_ID.search(link or '')
    return int(found.group(1)) if found else None


def normalize_checks(check_runs, combined_status):
    """Check runs and commit statuses as ``{name, bucket, link, conclusion, run_id,
    duration_s}`` (bucket: pass, fail, pending; conclusion: the run's conclusion, its status
    while it runs, or the status's state). ``run_id`` is the GitHub Actions run of a check run,
    read from its link: None for other check runs and for commit statuses. ``duration_s`` is
    what a completed check run took (:func:`run_seconds`, #300): None while it runs and for
    commit statuses, which have no duration.

    Of the check runs with the same name and app only the most recent counts
    (:func:`latest_check_runs`), as in GitHub's own rollup of a PR. Commit statuses are already
    one per context.

    ``combined_status`` is the response of ``commits/<sha>/status``. Its ``statuses`` list is
    paginated, so the aggregate ``state`` is authoritative: if it is worse than every listed
    status (a failing or pending context beyond the first page), it is added as its own entry.
    Without any status (``total_count`` 0) GitHub reports ``pending``, which means nothing here.
    """
    checks = []
    for run in latest_check_runs(check_runs):
        if run.get('status') != 'completed':
            bucket = 'pending'
        elif run.get('conclusion') in _FAILED_CONCLUSIONS:
            bucket = 'fail'
        else:
            bucket = 'pass'
        run_id = _RUN_ID.search(run.get('details_url') or '')
        checks.append({'name': run.get('name'), 'bucket': bucket, 'link': run.get('details_url'),
                       'conclusion': (run.get('conclusion') if run.get('status') == 'completed'
                                      else run.get('status')),
                       'run_id': int(run_id.group(1)) if run_id else None,
                       'duration_s': run_seconds(run)})
    statuses = combined_status.get('statuses') or []
    listed = [{'name': s.get('context'), 'bucket': _status_bucket(s.get('state')),
               'link': s.get('target_url'), 'conclusion': s.get('state'), 'run_id': None,
               'duration_s': None}
              for s in statuses]
    checks += listed
    if combined_status.get('total_count', len(statuses)):
        aggregate = _status_bucket(combined_status.get('state'))
        if aggregate != 'pass' and not any(c['bucket'] == aggregate for c in listed):
            checks.append({'name': 'commit status (aggregate)', 'bucket': aggregate, 'link': None,
                           'conclusion': combined_status.get('state'), 'run_id': None,
                           'duration_s': None})
    return checks
