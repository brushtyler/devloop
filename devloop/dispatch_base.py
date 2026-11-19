"""The health of the base branch (#459): the mixin ``BaseMixin`` of ``Dispatcher``.

**Tests red on the base too.** When the local tests of an implementation or of a fix fail,
before counting the failure against the implementer the dispatcher reads the identities of the
failed tests from the whole output (``localtests.failed_cases``) and runs their modules, one at
a time, on the base commit of the branch: the merge base of its HEAD with ``origin/<base>``, in
a disposable worktree (``Workspace.merge_base_copy``). Only when every failed test of the branch
fails there too is the step ``base_broken`` (``BaseBroken``): no other attempt of the agent, no
escalation. The dispatcher writes a ``basebroken`` record on the issue (implement) or on the PR
(fix), with the base commit and the failed tests, and the step waits for the base branch to move
past that commit, as a readiness check that depends on the repository (#270). An issue keeps
``agent:working`` and starts again in a free slot of ``max_open_prs``; the fix of a PR runs again
on its next tick. A failure only on the branch, identities that cannot be told (a timeout, an
output without them, a module that does not start), a project whose test selection is not
``python-unittest-modules`` (the check reads what unittest prints), or any error of the check
leaves the failure as it was: further attempts, then ``tests_failed``.

**Red CI on the base branch.** With ``[run].alert_issue``, at each tick that is not a dry run
the dispatcher reads the checks of the current commit of ``origin/<base>``; without it nothing
is read. When one ended with a failure it publishes one alert on ``[run].alert_issue`` with the
commit, the failed checks and the commits of the base branch since its latest green commit, at
most ``MAIN_HISTORY`` back on its first-parent chain (``Workspace.first_parents``). One alert
per commit: the commit alerted is kept in ``state_dir`` (``MainAlerts``), and with the outbox an
alert whose outcome is uncertain is only reconciled. Pending, missing or unreadable checks give
no alert. It is only a report: the merges go on as before.
"""

import json
import os
import threading
from pathlib import Path

from .github import GitHubError
from .localtests import UNITTEST_MODULES, case_modules, module_command
from .markdown import dispatcher_header, publishable
from .outbox import SENT as PUBLISHED
from .ports import summarize_checks
from .workspace import WorkspaceError

# The failed tests a record and its comment list at most; the count is always the whole.
MAX_CASES_LISTED = 30
# How many commits of the first-parent chain of the base branch the alert of a red CI walks back
# to find the latest green one, reading the checks of each (#459).
MAIN_HISTORY = 10


class BaseBroken:
    """The local tests of a writing turn failed only with tests that fail on the base commit of
    the branch too (#459): ``base``, that commit, ``cases``, the identities of the failed tests,
    and ``tail``, the last lines of the branch's run. A truthy failure of ``_write_turn``."""

    def __init__(self, base, cases, tail=''):
        self.base = base
        self.cases = list(cases)
        self.tail = tail


class MainAlerts:
    """The commit of the base branch whose red CI was last alerted (#459), in ``state_dir``
    (``FILE``) so that a restart does not alert it again; without ``state_dir`` it lasts as
    long as the process. A file that cannot be read counts as empty: the alert is published
    again rather than lost."""

    FILE = 'main-ci-alert.json'

    def __init__(self, state_dir=None):
        self._path = Path(state_dir) / self.FILE if state_dir is not None else None
        self._memory = None
        self._lock = threading.Lock()

    def alerted(self, sha):
        with self._lock:
            if self._path is None:
                return self._memory == sha
            try:
                data = json.loads(self._path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return False
            return isinstance(data, dict) and data.get('alerted') == sha

    def mark(self, sha):
        """Record ``sha`` as alerted; ``OSError`` when the file cannot be written."""
        with self._lock:
            self._memory = sha
            if self._path is None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f'.{self._path.name}.{os.getpid()}.tmp')
            tmp.write_text(json.dumps({'alerted': sha}) + '\n', encoding='utf-8')
            os.replace(tmp, self._path)


def _listed(cases):
    shown = ''.join(f'\n- `{c.replace("`", "")}`' for c in cases[:MAX_CASES_LISTED])
    more = len(cases) - MAX_CASES_LISTED
    return shown + (f'\n- … e altri {more}' if more > 0 else '')


class BaseMixin:
    """The checks of the base branch's health (#459). They use the dispatcher state and its
    other methods through ``self``."""

    # Tests red on the base too ------------------------------------------------
    def _base_broken(self, path, tail=''):
        """``BaseBroken`` when every test that failed in the latest local run in ``path`` fails
        on the base commit of its branch too, else None (logged): the failure is the branch's."""
        if self.cfg.tests.selection != UNITTEST_MODULES:
            self.log('  the failed tests are not compared with the base: the project declares '
                     f'no {UNITTEST_MODULES} selection')
            return None
        cases = self.ws.failed_cases(path)
        if not cases:
            self.log('  the failed tests cannot be identified from the output: the failure '
                     'counts against the branch')
            return None
        modules = case_modules(cases)
        if modules is None:
            self.log('  a failed test belongs to no test module: the failure counts against the '
                     'branch')
            return None
        try:
            name, copy, base = self.ws.merge_base_copy(path)
        except WorkspaceError as exc:
            self.log(f'  the base commit of the branch not checked out: {exc}')
            return None
        found = set()
        try:
            for module in modules:
                self.ws.run_tests(copy, module_command([module]), self.cfg.test_timeout_seconds)
                failed = self.ws.failed_cases(copy)
                if failed is None:
                    self.log(f'  {module} on the base commit {base[:12]}: its failed tests '
                             'cannot be identified, the failure counts against the branch')
                    return None
                found.update(failed)
        except WorkspaceError as exc:
            self.log(f'  the failed tests not run on the base commit {base[:12]}: {exc}')
            return None
        finally:
            self.ws.remove(name)
        only = [c for c in cases if c not in found]
        if only:
            self.log(f'  {len(only)} of {len(cases)} failed tests pass on the base commit '
                     f'{base[:12]}: the failure counts against the branch')
            return None
        self.log(f'  all {len(cases)} failed tests fail on the base commit {base[:12]} too: '
                 'base_broken')
        return BaseBroken(base, cases, tail)

    def _wait_base(self, number, step, broken, head):
        """Record on issue or PR ``number`` that ``step``, started from ``head``, waits for the
        base branch to move past ``broken.base`` (#459): a comment of the dispatcher with a
        ``basebroken`` record. No label changes: an issue keeps ``agent:working``, a PR its
        label."""
        base = self.cfg.base_branch
        what = 'dell\'issue' if step == 'implement' else 'della PR'
        text = (dispatcher_header()
                + '\n## Test rossi anche sulla base\n\n'
                f'Dopo il passo `{step}` i test locali falliscono, ma tutti i test falliti '
                f'falliscono anche sul commit base del branch, `{broken.base[:12]}`: il difetto è '
                f'di `{base}`, non della modifica {what}. Nessun altro tentativo di correzione e '
                f'nessuna escalation: il passo riparte da solo quando `{base}` avanza oltre quel '
                f'commit.\n\nTest falliti ({len(broken.cases)}):\n{_listed(broken.cases)}\n')
        if broken.tail:
            text += ('\n```text\n' + broken.tail.strip()[-6000:].replace('```', "'''")
                     + '\n```\n')
        payload = {'step': step, 'base': broken.base, 'head': head,
                   'count': len(broken.cases), 'cases': broken.cases[:MAX_CASES_LISTED]}
        self.gh.comment(number, publishable(text, 'basebroken', payload))
        self.log(f'  #{number}: {step} waits for {base} to move past {broken.base[:12]}')

    def _base_wait(self, comments, step, head=None):
        """The ``basebroken`` record of ``step`` that still holds in ``comments`` (#459), or
        None: the latest the dispatcher signed, on ``head`` when given (a PR's current head), with
        no notice of the dispatcher after it (an escalation, a block)."""
        records = self._dispatcher_records(comments, 'basebroken')
        if not records:
            return None
        position, record = records[-1]
        if record['step'] != step or head is not None and record['head'] != head:
            return None
        if any(p > position for p, _ in self._dispatcher_records(comments, 'notice')):
            return None
        return record

    def _base_holds(self, record, base_head):
        """Whether the wait ``record`` still holds: the base branch (``base_head()``, its
        current commit) has not moved past its commit. A base that cannot be read holds it."""
        try:
            return record['base'] == base_head()
        except WorkspaceError as exc:
            self.log(f'  the commit of {self.cfg.base_branch} not read, the wait holds: {exc}')
            return True

    # Red CI on the base branch ------------------------------------------------
    def _watch_main(self):
        """One alert on ``[run].alert_issue`` per commit of ``origin/<base>`` with a check
        concluded with a failure (#459). A failure to read is logged, and the next tick tries
        again. Without ``[run].alert_issue`` nothing is read. It is only a report: any error of
        it is logged by its class and never stops the tick."""
        issue = self.cfg.run.alert_issue
        if not issue:
            return
        try:
            self._watch_main_head(issue)
        except Exception as exc:  # only a report: its class, never its output
            self.log(f'  the CI of {self.cfg.base_branch} not watched ({type(exc).__name__}): '
                     'tried again at the next tick')

    def _watch_main_head(self, issue):
        base = self.cfg.base_branch
        try:
            head = self.ws.base_head()
        except WorkspaceError as exc:
            self.log(f'  the commit of {base} not read for its CI: {exc}')
            return
        if self.main_alerts.alerted(head):
            return
        try:
            failed = summarize_checks(self.gh.check_details(head))[1]
        except GitHubError as exc:
            self.log(f'  the CI of {base} at {head[:12]} not read: {exc}')
            return
        if not failed:
            return
        names = sorted(set(failed))
        if self._main_logged != head:
            self._main_logged = head
            self.log(f'WARNING: the CI of {base} fails at {head[:12]}: {", ".join(names)}')
        key = f'main_red:{head}'
        outbox = getattr(self.identities, 'outbox', None)
        try:
            state = outbox.alerts().get(key) if outbox is not None else None
        except GitHubError as exc:
            self.log(f'  the alert of the red CI of {base} cannot be checked: {exc}')
            return
        if state == PUBLISHED:
            self._mark_main(head, issue)
            return
        if state is not None:
            self.log(f'  the alert of the red CI of {base} on #{issue} has an uncertain outcome: '
                     'the outbox reconciles it, and no other alert is published meanwhile')
            return
        green, commits = self._since_green(head)
        try:
            self._alert(issue, self._main_body(head, names, green, commits), [key])
        except Exception as exc:  # one attempt per tick: its class, never its output
            self._alert_failed(f'the alert of the red CI of {base}', issue, exc, [key])
            return
        self._mark_main(head, issue)
        self.log(f'  alert of the red CI of {base} at {head[:12]} published on #{issue}')

    def _mark_main(self, head, issue):
        try:
            self.main_alerts.mark(head)
        except OSError as exc:
            self.log(f'  the alert of the red CI at {head[:12]} on #{issue} is not recorded: '
                     f'{exc}')

    def _since_green(self, head):
        """``(green, commits)``: the latest commit before ``head`` on the first-parent chain of
        the base branch whose CI passed, None when none of the ``MAIN_HISTORY`` read did, and
        the commits ``(sha, subject)`` after it up to ``head``, newest first. A chain or a CI
        that cannot be read ends the walk there."""
        try:
            chain = self.ws.first_parents(head, MAIN_HISTORY + 1)
        except WorkspaceError as exc:
            self.log(f'  the history of {self.cfg.base_branch} not read: {exc}')
            return None, []
        commits = []
        for sha, subject in chain:
            if sha != head:
                try:
                    state = summarize_checks(self.gh.check_details(sha))[0]
                except GitHubError as exc:
                    self.log(f'  the CI of {sha[:12]} not read: {exc}')
                    return None, commits
                if state == 'pass':
                    return sha, commits
            commits.append((sha, subject))
        return None, commits

    def _main_body(self, head, names, green, commits):
        base = self.cfg.base_branch
        checks = ''.join(f'\n- `{n.replace("`", "")}`' for n in names)
        listed = ''.join(f'\n- `{sha[:12]}` {subject.replace("`", "")}'
                         for sha, subject in commits)
        since = (f'Commit di `{base}` dall\'ultimo commit verde, `{green[:12]}`:' if green else
                 f'Nessun commit verde fra gli ultimi {MAIN_HISTORY} di `{base}` letti; i commit '
                 'letti:')
        return publishable(
            dispatcher_header()
            + f'\n## CI rossa su `{base}`\n\n'
            + f'Sul commit `{head[:12]}` di `{base}` questi check sono conclusi con un '
              f'fallimento:\n{checks}\n\n{since}\n{listed}\n\n'
            + 'È solo una segnalazione: i merge proseguono come prima. Un nuovo allarme parte '
              f'solo per un nuovo commit rosso di `{base}`.')
