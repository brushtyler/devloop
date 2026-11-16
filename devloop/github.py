"""Thin wrapper over the `gh` CLI. All GitHub state devloop relies on goes through here.

Checks, labels, comments and pull request creation use the REST API through `gh api`, which
behaves the same across `gh` versions. Porcelain commands are used only for reads whose `--json`
output is long-standing (`issue list/view`, `pr list/view`).
"""

import json
import os
import re
import subprocess
from urllib.parse import quote

_RUN_ID = re.compile(r'/actions/runs/(\d+)')
_ISSUE_HISTORY = (
    'query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){'
    'issue(number:$number){title body author{login} lastEditedAt editor{login} '
    'timelineItems(itemTypes:[LABELED_EVENT,RENAMED_TITLE_EVENT],last:100){nodes{__typename '
    '... on LabeledEvent{createdAt actor{login} label{name}} '
    '... on RenamedTitleEvent{createdAt actor{login}}}}}}}')
_FAILED_CONCLUSIONS = {'failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure',
                       'stale'}
_FAILED_STATUSES = {'failure', 'error'}


class GitHubError(RuntimeError):
    pass


def _login(who):
    return (who or {}).get('login')


def author_login(author):
    """A login in REST form. `gh` porcelain reports an app as ``app/<slug>``; REST and the
    trust checks use ``<slug>[bot]``."""
    login = _login(author)
    if login and (author or {}).get('is_bot') and login.startswith('app/'):
        return login[len('app/'):] + '[bot]'
    return login


class GitHub:
    """``token``, when given, is a callable returning the token every call runs with; otherwise
    the ambient ``gh`` login is used."""

    def __init__(self, repo, run=subprocess.run, token=None):
        self.repo = repo
        self._run = run
        self._token = token

    def _gh(self, *args, stdin=None, check=True):
        kwargs = {}
        if self._token is not None:
            env = {k: v for k, v in os.environ.items() if k not in ('GITHUB_TOKEN', 'GH_TOKEN')}
            kwargs['env'] = dict(env, GH_TOKEN=self._token())
        try:
            done = self._run(['gh', *args], input=stdin, capture_output=True, text=True,
                             timeout=300, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(f'gh {" ".join(args[:3])}: timed out after {exc.timeout}s') from exc
        except OSError as exc:
            raise GitHubError(f'gh could not start: {exc}') from exc
        if check and done.returncode != 0:
            raise GitHubError(f'gh {" ".join(args[:3])}: {done.stderr.strip()[:500]}')
        return done

    def _json(self, *args):
        return json.loads(self._gh(*args).stdout or 'null')

    def _json_lines(self, *args):
        return [json.loads(line) for line in self._gh(*args).stdout.splitlines() if line.strip()]

    # Reads ---------------------------------------------------------------
    def issues_with_label(self, label):
        return self._json('issue', 'list', '-R', self.repo, '--state', 'open', '--label', label,
                          '--limit', '200', '--json', 'number,title,labels')

    def issue(self, number):
        return self._json('issue', 'view', str(number), '-R', self.repo,
                          '--json', 'number,title,body,labels,comments,state')

    def issue_history(self, number):
        """Title, body and who changed them, read in one query so that the text and its history
        match: ``{title, body, author, last_edited_at, editor, labeled, renamed}``. ``labeled`` is
        a list of ``(label, login, at)`` and ``renamed`` of ``(login, at)``, oldest first."""
        owner, name = self.repo.split('/', 1)
        data = self._json('api', 'graphql', '-f', f'query={_ISSUE_HISTORY}', '-f', f'owner={owner}',
                          '-f', f'name={name}', '-F', f'number={int(number)}')
        issue = data['data']['repository']['issue']
        nodes = issue['timelineItems']['nodes']
        return {
            'title': issue['title'], 'body': issue['body'] or '',
            'author': _login(issue['author']), 'last_edited_at': issue['lastEditedAt'],
            'editor': _login(issue['editor']),
            'labeled': [((n.get('label') or {}).get('name'), _login(n['actor']), n['createdAt'])
                        for n in nodes if n['__typename'] == 'LabeledEvent'],
            'renamed': [(_login(n['actor']), n['createdAt'])
                        for n in nodes if n['__typename'] == 'RenamedTitleEvent'],
        }

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

    def pr_comments(self, number):
        """Conversation comments, read with REST so that an app's login is ``<slug>[bot]``
        (`gh pr view` reports it as a bare ``<slug>``, indistinguishable from a user)."""
        return self._json_lines('api', '--paginate', f'repos/{self.repo}/issues/{number}/comments',
                                '--jq', '.[] | {id, author: {login: .user.login}, body}')

    def issue_comments(self, number):
        """An issue's comments, oldest first, read with REST as :meth:`pr_comments`."""
        return self.pr_comments(number)

    def pr_head(self, number):
        """The PR's current head commit, read fresh."""
        return self._json('api', f'repos/{self.repo}/pulls/{number}')['head']['sha']

    def checks(self, sha):
        """``(state, failed_names, failed_run_ids)`` for commit ``sha``.

        ``state`` is pass, fail, pending or none. Both check runs (Actions) and commit statuses
        count.
        """
        return summarize_checks(self.check_details(sha))

    def check_details(self, sha):
        """Every check of commit ``sha`` as ``normalize_checks`` returns it: check runs and
        commit statuses, each with its outcome and, for a GitHub Actions run, its run id."""
        runs = self._json_lines('api', '--paginate', f'repos/{self.repo}/commits/{sha}/check-runs',
                                '--jq', '.check_runs[] | {name, status, conclusion, details_url}')
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

    def pr_reviews(self, number):
        """The native reviews of PR ``number``, oldest first, as ``{id, login, state}`` (state:
        APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING)."""
        return self._json_lines('api', '--paginate',
                                f'repos/{self.repo}/pulls/{number}/reviews?per_page=100',
                                '--jq', '.[] | {id, login: .user.login, state}')

    def failed_log(self, run_id, max_lines=80):
        done = self._gh('run', 'view', str(run_id), '-R', self.repo, '--log-failed', check=False)
        return '\n'.join(done.stdout.splitlines()[-max_lines:])

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
        """Post a comment; returns its id, or None if the response does not carry one."""
        done = self._gh('api', '-X', 'POST', f'repos/{self.repo}/issues/{number}/comments',
                        '-F', 'body=@-', stdin=body)
        try:
            created = json.loads(done.stdout or 'null')
        except json.JSONDecodeError:
            return None
        return created.get('id') if isinstance(created, dict) else None

    def edit_comment(self, comment_id, body):
        """Replace the body of comment ``comment_id`` of an issue or a PR."""
        self._gh('api', '-X', 'PATCH', f'repos/{self.repo}/issues/comments/{int(comment_id)}',
                 '-F', 'body=@-', stdin=body)

    def create_pr(self, *, base, head, title, body):
        """``(number, url)`` of the new PR. Labels are the dispatcher's job, not the author's."""
        created = json.loads(self._gh(
            'api', '-X', 'POST', f'repos/{self.repo}/pulls', '-f', f'title={title}',
            '-f', f'head={head}', '-f', f'base={base}', '-F', 'body=@-', stdin=body).stdout)
        return created['number'], created['html_url']

    def submit_review(self, number, *, commit, event, body):
        """A native PR review (APPROVE, REQUEST_CHANGES or COMMENT) on ``commit``."""
        self._gh('api', '-X', 'POST', f'repos/{self.repo}/pulls/{number}/reviews',
                 '-f', f'commit_id={commit}', '-f', f'event={event}', '-F', 'body=@-', stdin=body)

    def ensure_label(self, name, color, description):
        self._gh('label', 'create', name, '-R', self.repo, '--color', color,
                 '--description', description, '--force')


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


def normalize_checks(check_runs, combined_status):
    """Check runs and commit statuses as ``{name, bucket, link, conclusion, run_id}`` (bucket:
    pass, fail, pending; conclusion: the run's conclusion, its status while it runs, or the
    status's state). ``run_id`` is the GitHub Actions run of a check run, read from its link:
    None for other check runs and for commit statuses.

    ``combined_status`` is the response of ``commits/<sha>/status``. Its ``statuses`` list is
    paginated, so the aggregate ``state`` is authoritative: if it is worse than every listed
    status (a failing or pending context beyond the first page), it is added as its own entry.
    Without any status (``total_count`` 0) GitHub reports ``pending``, which means nothing here.
    """
    checks = []
    for run in check_runs:
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
                       'run_id': int(run_id.group(1)) if run_id else None})
    statuses = combined_status.get('statuses') or []
    listed = [{'name': s.get('context'), 'bucket': _status_bucket(s.get('state')),
               'link': s.get('target_url'), 'conclusion': s.get('state'), 'run_id': None}
              for s in statuses]
    checks += listed
    if combined_status.get('total_count', len(statuses)):
        aggregate = _status_bucket(combined_status.get('state'))
        if aggregate != 'pass' and not any(c['bucket'] == aggregate for c in listed):
            checks.append({'name': 'commit status (aggregate)', 'bucket': aggregate, 'link': None,
                           'conclusion': combined_status.get('state'), 'run_id': None})
    return checks


def summarize_checks(checks):
    if not checks:
        return 'none', [], []
    failed = [c for c in checks if c.get('bucket') == 'fail']
    if failed:
        run_ids = sorted({m.group(1) for c in failed if (m := _RUN_ID.search(c.get('link') or ''))})
        return 'fail', [c['name'] for c in failed], run_ids
    if any(c.get('bucket') == 'pending' for c in checks):
        return 'pending', [], []
    return 'pass', [], []
