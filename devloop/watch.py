"""``devloop watch`` (#173): wakes the agent that supervises devloop when GitHub changes.

Every ``interval`` seconds a round reads the open PRs and issues and the comments written since
the previous round, compares them with the state of the latest successful round, and turns what
changed into lines ``HH:MM <type> #N <detail>`` (UTC):

- labels added and removed, on issues and PRs;
- a new head of a PR, and its CI turning ``pass`` or ``fail`` (``GitHub.checks``, as the
  dispatcher reads it);
- reviews with a verdict: devloop's from the valid ``review`` record in the conversation, written
  by the identity that may write it, or in the body of the native review of an agent with its
  own app (#325), reported with its verdict and round; a trusted user's native ``APPROVED`` or
  ``CHANGES_REQUESTED``;
- comments of trusted users and of devloop's apps, those a trusted user wrote in person marked
  ``operator comment``; ledger records (#133) are left out;
- issues and PRs opened, closed or merged.

Trust follows AGENTS.md. An issue or PR counts if its author is in ``trusted_users`` or is one of
devloop's apps. Anyone else's produces only ``new <type> #N by an untrusted author (ignored)``
and nothing after it, except an issue whose latest ``agent:ready`` a trusted user added, still
there or replaced since: while so its labels, closure and trusted comments are reported, never its
title. Labels, heads, CI and closures of trusted objects are reported whoever changed them;
comments and reviews only when their author is trusted or one of devloop's apps. A trusted comment
on an issue or PR outside the open listings (closed before the baseline) is reported too: its
author is read once; on an external issue the latest ``agent:ready`` is read again in each round
with a new trusted comment on it, since the admission can change while the issue stays closed.

Rounds compare states, so a change undone between two reads (a label added and removed) is not
seen. Comments and reviews have persistent ids: each one still there is seen.

Two modes. ``until_change`` (default) ends at the first round with news, printing its lines:
exit 0; with a ``timeout`` it exits with ``NOTHING_NEW`` once that many seconds passed since the
start (monotonic clock, failed rounds included) without news. Each ``gh`` call gets at most the
time left, and a round that ends after the timeout is dropped unsaved. ``exec`` keeps running
and, after each round with news, runs a command (``shlex.split``, no shell) with the lines on
its stdin, waiting for it to end: one at a time. The state is saved, atomically, before the
lines are printed or delivered, so a line is never repeated but an interruption in between
loses it.

Three consecutive failed rounds give one ``ERROR_LINE``, which counts as news; the state of the
latest successful round stays, and a successful round resets the count. Each failed round is
reported on stderr with the class of its error (``failure_class``), never with ``gh``'s output.
"""

import json
import re
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .apps import DISPATCHER, MERGER, AppAuth, Identities
from .dispatch_actions import ready_approval
from .github import GitHubError, GitHubNotFound, author_login
from .markdown import read_marker
from .planning import label_names

STATE_VERSION = 3
# In the state's ``closed``: an issue of an untrusted author, whose admission is read again.
EXTERNAL = 'external'
STATE_FILE = 'watch-state.json'
NOTHING_NEW = 3  # exit code of --until-change at --timeout
EXPIRED = object()  # a round that ended after the timeout
ERROR_ROUNDS = 3
ERROR_LINE = 'watch error: 3 consecutive rounds could not read GitHub, the last valid state is kept'
VERDICTS = ('APPROVED', 'CHANGES_REQUESTED')
LEDGER_TITLE = '## Registro devloop'
# Comments are read from this long before the previous round, against clock skew; their ids keep
# them from being reported twice.
COMMENT_MARGIN = timedelta(minutes=5)
MAX_COMMENT_IDS = 2000
MAX_CLOSED = 500
TITLE_LEN = 70
TEXT_LEN = 110
# The class of a failed read, from the message of its GitHubError (``gh``'s stderr, or the HTTP
# error of the app token), first match wins. Only the class is printed: no token, path or text.
FAILURE_CLASSES = (
    ('rate limit', re.compile(r'rate limit|HTTP 429', re.I)),
    ('authentication', re.compile(r'HTTP 401|bad credentials|requires authentication|'
                                  r'gh auth login|not logged in|authentication|'
                                  r'could not sign the app JWT', re.I)),
    ('permission', re.compile(r'HTTP 403|not accessible by integration', re.I)),
    ('not found', re.compile(r'HTTP 404|HTTP 410|could not resolve to an? ', re.I)),
    ('server error', re.compile(r'HTTP 5\d\d', re.I)),
    ('network', re.compile(r'timed out|error connecting|could not resolve host|no such host|'
                           r'connection (?:refused|reset)|dial tcp|tls handshake|'
                           r'urlopen error', re.I)),
    ('gh unavailable', re.compile(r'gh could not start', re.I)),
    # A `--json` field the installed `gh` does not know (#211).
    ('cli_version', re.compile(r'Unknown JSON field', re.I)),
)


def failure_class(exc):
    """What kind of failure ``exc`` is, for the ``round failed`` line: ``rate limit``,
    ``authentication``, ``permission``, ``not found``, ``server error``, ``network``,
    ``gh unavailable`` or ``cli_version`` for a GitHub error it recognises, else the name of its
    type."""
    if isinstance(exc, GitHubNotFound):
        return 'not found'
    if isinstance(exc, GitHubError):
        text = str(exc)
        for name, pattern in FAILURE_CLASSES:
            if pattern.search(text):
                return name
    return type(exc).__name__


class Trust:
    """Who counts for the watcher: ``users`` (``trusted_users``) and ``writers``, the app login
    of each actor that has one (agents and the dispatcher). The ``merger`` app (#201) only
    merges: it is never a writer, so its comments and records are not trusted."""

    def __init__(self, users, writers, ready_label):
        self.users = frozenset(users)
        self.writers = {actor: login for actor, login in dict(writers).items() if actor != MERGER}
        self.apps = frozenset(self.writers.values())
        self.ready_label = ready_label

    @classmethod
    def from_config(cls, cfg):
        """The logins of the configured apps, the agents' and the dispatcher's, as the
        dispatcher derives them (``Identities.login``); not the ``merger``'s, which writes
        nothing. Only slugs are needed: no private key is read."""
        identities = Identities(cfg.repo, {actor: AppAuth(app.app_id, app.slug, None, cfg.repo)
                                           for actor, app in cfg.apps.items()
                                           if actor != MERGER})
        writers = {actor: identities.login(actor) for actor in identities.apps}
        return cls(cfg.trusted_users, writers, cfg.labels.ready)

    def trusted(self, login):
        return bool(login) and (login in self.users or login in self.apps)

    def wrote(self, login, actor):
        """Whether ``login`` may write records for ``actor``: its app, else a trusted user.
        Nobody writes records for the ``merger``."""
        if actor == MERGER:
            return False
        expected = self.writers.get(actor)
        return login == expected if expected else login in self.users


def clean(text, limit):
    """One line of at most ``limit`` characters, without control characters."""
    text = ''.join(' ' if unicodedata.category(ch).startswith('C') else ch for ch in text or '')
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def first_line(body):
    """The first line of a comment that is not blank nor part of a quote (the AGENTS.md header
    is one)."""
    for line in (body or '').splitlines():
        line = line.strip()
        if line and not line.startswith('>'):
            return line
    return ''


def _stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def _visible(entry):
    """Whether an issue or PR of the state is reported: a trusted author, or admitted."""
    return bool(entry.get('trusted') or entry.get('admitted'))


def _label_change(kind, number, old, new):
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    if not (added or removed):
        return []
    return [f'{kind} #{number} labels '
            + ' '.join([f'+{name}' for name in added] + [f'-{name}' for name in removed])]


def _labels(labels):
    return '[' + ','.join(labels) + ']'


def _review_lines(number, cur, known_reviews):
    """Lines for the native reviews of PR ``cur`` whose id is not among ``known_reviews``."""
    known = {review[0] for review in known_reviews}
    return [f'PR #{number} review by {login}: {verdict} @{cur["head"][:7]}'
            for rid, login, verdict in cur['reviews'] if rid not in known]


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_number(key):
    return isinstance(key, str) and key.isascii() and key.isdigit()


def _valid_entry(kind, entry):
    """Whether ``entry`` is an issue or PR as ``Watcher._read_prs`` and ``_read_issues`` write
    it, down to the types the comparison uses."""
    if not (isinstance(entry, dict) and isinstance(entry.get('trusted'), bool)
            and _is_strings(entry.get('labels'))):
        return False
    if entry['trusted']:
        if not isinstance(entry.get('title'), str):
            return False
        if kind == 'issues':
            return True
        reviews = entry.get('reviews')
        return (isinstance(entry.get('head'), str) and isinstance(entry.get('ci'), str)
                and isinstance(reviews, list)
                and all(isinstance(review, list) and len(review) == 3 and _is_int(review[0])
                        and isinstance(review[1], str) and isinstance(review[2], str)
                        for review in reviews))
    if kind == 'prs':
        return True
    admitted = entry.get('admitted')
    return isinstance(admitted, bool) and isinstance(entry.get('by'),
                                                     str if admitted else (str, type(None)))


def valid_state(state):
    """Whether ``state`` has the structure the watcher writes, down to the types of the items
    the comparison reads: a state that passes cannot make every round fail."""
    if not isinstance(state, dict) or state.get('version') != STATE_VERSION:
        return False
    prs, issues, closed, comments = (state.get(key)
                                     for key in ('prs', 'issues', 'closed', 'comments'))
    if not all(isinstance(part, dict) for part in (prs, issues, closed, comments)):
        return False
    if not all(_is_number(number) for part in (prs, issues, closed) for number in part):
        return False
    if not (all(_valid_entry('prs', entry) for entry in prs.values())
            and all(_valid_entry('issues', entry) for entry in issues.values())
            and all(kind in ('PR', 'issue', EXTERNAL, None) for kind in closed.values())):
        return False
    seen, since = comments.get('seen'), comments.get('since')
    if not (_is_int(comments.get('floor')) and isinstance(seen, list)
            and all(_is_int(cid) for cid in seen) and isinstance(since, str)):
        return False
    try:
        datetime.strptime(since, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return False
    return True


def load_state(path):
    """The saved state, or None when it is missing or unusable (a new baseline replaces it)."""
    try:
        state = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return state if valid_state(state) else None


def save_state(path, state):
    """Write ``state`` atomically: a temporary file in the same directory, then a rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Watcher:
    """``gh`` reads GitHub (``github.GitHub``). ``exec_argv`` selects the ``exec`` mode; without
    it the watcher runs ``until_change``. ``clock`` is monotonic, ``now`` the UTC wall time of the
    lines and of the comment reads."""

    def __init__(self, gh, trust, state_path, *, interval=60, timeout=None, exec_argv=None,
                 run=subprocess.run, clock=time.monotonic, sleep=time.sleep,
                 now=lambda: datetime.now(timezone.utc), out=None, err=None):
        self.gh = gh
        self.trust = trust
        self.state_path = Path(state_path)
        self.interval = interval
        self.timeout = timeout
        self.exec_argv = exec_argv
        self._run = run
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._out = out or (lambda line: print(line, flush=True))
        self._err = err or (lambda line: print(line, file=sys.stderr, flush=True))
        self.state = None
        self._deadline = None

    def time_left(self):
        """Seconds left before the ``timeout`` (None without one). ``github.GitHub`` bounds each
        ``gh`` call with it (``time_left``) and starts none once it is spent."""
        return None if self._deadline is None else self._deadline - self._clock()

    def _expired(self):
        left = self.time_left()
        return left is not None and left <= 0

    # Loop ----------------------------------------------------------------
    def run(self):
        """``until_change``: 0 at the first round with news, ``NOTHING_NEW`` at the timeout.
        ``exec``: runs until interrupted."""
        start = self._clock()
        self._deadline = start + self.timeout if self.timeout else None
        self.state = load_state(self.state_path)
        if self.state is None and self.state_path.exists():
            self._err('devloop watch: the state file is unreadable, a new baseline replaces it')
        failures = 0
        while True:
            lines = self.round()
            if lines is EXPIRED:
                return NOTHING_NEW
            if lines is None:
                failures += 1
                lines = [ERROR_LINE] if failures == ERROR_ROUNDS else []
            else:
                failures = 0
            if lines:
                stamp = self._now().strftime('%H:%M')
                lines = [f'{stamp} {line}' for line in lines]
                for line in lines:
                    self._out(line)
                if self.exec_argv is None:
                    return 0
                self._deliver(lines)
            elapsed = self._clock() - start
            if self.timeout and elapsed >= self.timeout:
                return NOTHING_NEW
            wait = self.interval if not self.timeout else min(self.interval,
                                                              self.timeout - elapsed)
            self._sleep(max(wait, 0))

    def _deliver(self, lines):
        """Run the ``exec`` command once with ``lines`` on its stdin, waiting for it. A command
        that does not start or fails is reported; its lines are not delivered again."""
        try:
            done = self._run(self.exec_argv, input='\n'.join(lines) + '\n', text=True)
        except OSError as exc:
            self._err(f'devloop watch: the --exec command did not start: '
                      f'{exc.strerror or type(exc).__name__}')
            return
        if done.returncode != 0:
            self._err(f'devloop watch: the --exec command exited with {done.returncode}')

    def round(self):
        """One round: its lines (empty without news), None when it failed, ``EXPIRED`` when
        the timeout passed before it ended. The state is saved before the lines are returned;
        a failed or expired round changes nothing, so what it read is reported by a later
        run."""
        started = self._now()
        summary = None
        try:
            if self.state is None:
                state, summary = self._baseline(started)
                lines = []
            else:
                lines, state = self._compare(started)
            if self._expired():
                return EXPIRED
            save_state(self.state_path, state)
        except (GitHubError, OSError, ValueError, KeyError, TypeError) as exc:
            if self._expired():
                return EXPIRED
            self._err(f'devloop watch: round failed ({failure_class(exc)})')
            return None
        self.state = state
        if summary:
            self._out(f'{started:%H:%M} {summary}')
        return lines

    # Reads -----------------------------------------------------------------
    def _read_prs(self):
        prs = {}
        for raw in self.gh.open_prs():
            trusted = self.trust.trusted((raw.get('author') or {}).get('login'))
            entry = {'trusted': trusted, 'labels': sorted(label_names(raw.get('labels') or []))}
            if trusted:
                head = raw['headRefOid']
                reviews = []
                for r in self.gh.pr_reviews(raw['number']):
                    record = self._native_record(r)
                    if record is not None:
                        reviews.append([r['id'], r['login'], record])
                    elif r.get('login') in self.trust.users and r.get('state') in VERDICTS:
                        reviews.append([r['id'], r['login'], r['state']])
                entry.update(title=clean(raw.get('title'), TITLE_LEN), head=head,
                             ci=self.gh.checks(head)[0], reviews=reviews)
            prs[str(raw['number'])] = entry
        return prs

    def _native_record(self, review):
        """``<verdict> round <n>`` of a native review that is the ``review`` record of an agent
        (#325), written by the identity that may write it; None for any other review."""
        record = read_marker(review.get('body') or '', 'review')
        if record is None or record['source'] != 'agent':
            return None
        actor = record.get('reviewer')
        if not (isinstance(actor, str) and self.trust.wrote(review.get('login'), actor)):
            return None
        return f'{record["verdict"]} round {record["round"]}'

    def _read_issues(self, previous):
        issues = {}
        for raw in self.gh.open_issues():
            number = str(raw['number'])
            trusted = self.trust.trusted(author_login(raw.get('author')))
            labels = sorted(label_names(raw.get('labels') or []))
            entry = {'trusted': trusted, 'labels': labels}
            if trusted:
                entry['title'] = clean(raw.get('title'), TITLE_LEN)
            else:
                entry.update(self._admission(number, labels, previous.get(number) or {}))
            issues[number] = entry
        return issues

    def _admission(self, number, labels, previous):
        """Whether an issue of an untrusted author is admitted: the latest ``labeled`` event of
        ``agent:ready`` comes from a trusted user, whether the label is still there or was
        replaced since (``agent:working``). A new ``agent:ready`` changes the labels, so the
        events are read when the issue is first seen and whenever its labels differ from the
        previous read; a label removed and added again between two reads is not seen."""
        if 'admitted' in previous and previous.get('labels') == labels:
            return {'admitted': previous['admitted'], 'by': previous.get('by')}
        approved = self._approved(number)
        return {'admitted': bool(approved), 'by': approved[0] if approved else None}

    def _approved(self, number):
        """``(login, at)`` of the trusted user who added the latest ``agent:ready`` of issue
        ``number``, or None."""
        events = self.gh.labeled_events(int(number))
        return ready_approval([(e.get('label'), e.get('by'), e.get('at')) for e in events],
                              self.trust.ready_label, self.trust.users)

    def _read_comments(self, since):
        return sorted(self.gh.comments_since(since), key=lambda c: c['id'])

    # Baseline and comparison -----------------------------------------------
    def _baseline(self, started):
        prs = self._read_prs()
        issues = self._read_issues({})
        comments = self._read_comments(_stamp(started - COMMENT_MARGIN))
        seen = [c['id'] for c in comments]
        state = {'version': STATE_VERSION, 'prs': prs, 'issues': issues, 'closed': {},
                 'comments': {'floor': max(seen, default=0), 'seen': [],
                              'since': _stamp(started - COMMENT_MARGIN)}}
        return state, f'watch baseline: {len(prs)} open PRs, {len(issues)} open issues'

    def _compare(self, started):
        old = self.state
        prs = self._read_prs()
        issues = self._read_issues(old['issues'])
        comments = self._read_comments(old['comments']['since'])
        closed = dict(old['closed'])
        lines = []
        lines += self._pr_lines(old['prs'], prs)
        lines += self._issue_lines(old['issues'], issues)
        lines += self._closed_lines('PR', old['prs'], prs, closed)
        lines += self._closed_lines('issue', old['issues'], issues, closed)
        comment_lines, comment_state = self._comment_lines(old['comments'], comments,
                                                           old, prs, issues, closed)
        lines += comment_lines
        comment_state['since'] = _stamp(started - COMMENT_MARGIN)
        while len(closed) > MAX_CLOSED:
            closed.pop(min(closed, key=int))
        return lines, {'version': STATE_VERSION, 'prs': prs, 'issues': issues, 'closed': closed,
                       'comments': comment_state}

    def _pr_lines(self, old, new):
        lines = []
        for number, cur in new.items():
            prev = old.get(number)
            if prev is None:
                if not cur['trusted']:
                    lines.append(f'new PR #{number} by an untrusted author (ignored)')
                    continue
                # Opened since the previous round: its reviews are new too.
                lines.append(f'new PR #{number} {_labels(cur["labels"])} {cur["title"]}')
                lines += _review_lines(number, cur, ())
                continue
            if not (cur['trusted'] and prev['trusted']):
                continue
            lines += _label_change('PR', number, prev['labels'], cur['labels'])
            if cur['head'] != prev.get('head'):
                lines.append(f'PR #{number} new head {cur["head"][:7]}')
            if (cur['ci'] in ('pass', 'fail')
                    and (cur['ci'], cur['head']) != (prev.get('ci'), prev.get('head'))):
                lines.append(f'PR #{number} CI {cur["ci"]} @{cur["head"][:7]}')
            lines += _review_lines(number, cur, prev.get('reviews') or ())
        return lines

    def _issue_lines(self, old, new):
        lines = []
        for number, cur in new.items():
            prev = old.get(number)
            if prev is None:
                if cur['trusted']:
                    lines.append(f'new issue #{number} {_labels(cur["labels"])} {cur["title"]}')
                elif cur.get('admitted'):
                    lines.append(f'new issue #{number} by an untrusted author, admitted with '
                                 f'{self.trust.ready_label} by {cur["by"]} '
                                 f'{_labels(cur["labels"])}')
                else:
                    lines.append(f'new issue #{number} by an untrusted author (ignored)')
                continue
            if not _visible(cur):
                continue
            if _visible(prev):
                lines += _label_change('issue', number, prev['labels'], cur['labels'])
            else:
                lines.append(f'issue #{number} admitted with {self.trust.ready_label} by '
                             f'{cur["by"]} {_labels(cur["labels"])}')
        return lines

    def _closed_lines(self, kind, old, new, closed):
        """Lines for the visible issues or PRs no longer open. One still open but beyond the
        listing's limit stays in the state as it was. Only GitHub's answer that the object does
        not exist (deleted, transferred) gives ``no longer open``; a failed read fails the
        round, so the next one asks again."""
        lines = []
        for number, prev in old.items():
            if number in new or not _visible(prev):
                continue
            try:
                if kind == 'PR':
                    outcome = (self.gh.pr_state(int(number)).get('state') or '').lower()
                else:
                    read = self.gh.issue_state(int(number))
                    outcome = (read.get('state') or '').lower()
                    if outcome == 'closed' and read.get('stateReason'):
                        outcome += f' ({read["stateReason"].lower()})'
            except GitHubNotFound:
                outcome = 'no longer open'  # deleted or transferred
            if outcome == 'open':
                new[number] = prev
                continue
            title = f': {prev["title"]}' if prev.get('title') else ''
            lines.append(f'{kind} #{number} {outcome or "no longer open"}{title}')
            # An admitted external issue keeps its admission open to a later agent:ready.
            closed[number] = kind if prev.get('trusted') else EXTERNAL
        return lines

    def _comment_lines(self, previous, comments, old, prs, issues, closed):
        floor = previous['floor']
        seen = set(previous['seen'])
        approvals = {}  # admissions of closed external issues read in this round
        lines = []
        for comment in comments:
            cid = comment['id']
            if cid <= floor or cid in seen:
                continue
            seen.add(cid)
            login = comment.get('login')
            if not self.trust.trusted(login):
                continue
            number = str(comment['number'])
            kind = self._object_kind(number, old, prs, issues, closed, approvals)
            if kind is None:
                continue  # an untrusted author's issue or PR, not admitted
            line = self._comment_line(kind, number, login, comment.get('body') or '')
            if line:
                lines.append(line)
        kept = sorted(seen)
        if len(kept) > MAX_COMMENT_IDS:
            floor = max(floor, kept[-MAX_COMMENT_IDS - 1])
            kept = kept[-MAX_COMMENT_IDS:]
        return lines, {'floor': floor, 'seen': kept}

    def _object_kind(self, number, old, prs, issues, closed, approvals):
        """``PR`` or ``issue`` for a visible issue or PR, None otherwise. For one outside the
        open listings and the state (closed before the baseline, beyond the listing's limit)
        type and author are read once and remembered in ``closed``; the admission of an
        external issue is not, since a later ``agent:ready`` can grant or end it: it is read
        again, once per round (``approvals``)."""
        for kind, current, before in (('PR', prs, old['prs']), ('issue', issues, old['issues'])):
            entry = current.get(number) or before.get(number)
            if entry is not None:
                return kind if _visible(entry) else None
        if number not in closed:
            closed[number] = self._resolve(number)
        kind = closed[number]
        if kind != EXTERNAL:
            return kind
        if number not in approvals:
            approvals[number] = bool(self._approved(number))
        return 'issue' if approvals[number] else None

    def _resolve(self, number):
        """``PR`` or ``issue`` for issue or PR ``number`` of a trusted author, ``EXTERNAL`` for
        an issue of an untrusted author (its admission is read apart), None for an untrusted
        author's PR or when it does not exist."""
        try:
            read = self.gh.issue_author(int(number))
        except GitHubNotFound:
            return None
        kind = 'PR' if read.get('pull_request') else 'issue'
        if self.trust.trusted(read.get('login')):
            return kind
        return EXTERNAL if kind == 'issue' else None

    def _comment_line(self, kind, number, login, body):
        if read_marker(body, 'ledger') is not None or first_line(body).startswith(LEDGER_TITLE):
            return None  # the ledger (#133): recorded on GitHub, too frequent to report
        review = read_marker(body, 'review')
        if review is not None:
            actor = DISPATCHER if review['source'] == 'ci' else review.get('reviewer')
            if isinstance(actor, str) and self.trust.wrote(login, actor):
                who = 'ci' if review['source'] == 'ci' else actor
                return (f'{kind} #{number} review {who} {review["verdict"]} round '
                        f'{review["round"]} @{review["head_sha"][:7]}')
        operator = login in self.trust.users and not body.lstrip().startswith('> [!NOTE]')
        what = 'operator comment' if operator else 'comment'
        return f'{kind} #{number} {what} by {login}: {clean(first_line(body), TEXT_LEN)}'


def parse_exec(command):
    """The argv of ``--exec``, split as a shell would but run without one."""
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f'--exec: {exc}') from exc
    if not argv:
        raise ValueError('--exec: empty command')
    return argv
