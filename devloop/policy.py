"""Review policy: the review level a devloop PR needs, and the receipt that binds a review or a
promotion to the policy it was given under (issue #106, part 1 of #66).

The policy is the running dispatcher's: its code and its configuration, never the PR branch,
so a PR that changes ``devloop/config.toml`` or the prompts cannot apply weaker rules to itself.

The effective level of a head is the highest of:

- the configured default (``default_level``, at least 1);
- level 2 when a user in ``trusted_users`` ever added ``label`` to the PR (every ``labeled``
  event counts, so removing the label later does not lower the level);
- level 2 when the PR's full diff against the base touches a sensitive path;
- the highest level already recorded on the PR by an earlier receipt.

Nothing lowers a level, and nothing takes it below 1, except level 0.

Level 0 (#107, part 2 of #66) needs no review. It is off unless the operator configures both an
allowlist of paths and the expected CI check runs, and it applies only when:

- the latest ``labeled`` or ``unlabeled`` event of ``zero_label`` on the PR added it, and a user
  in ``trusted_users`` wrote it;
- the level computed above is exactly 1: the level-2 label, a sensitive path and a level 2
  already recorded always prevail;
- the whole diff is in the allowlist (``zero_allowed``);
- no receipt recorded on the PR refused that label event already: once a head left the
  allowlist, only a newer label brings the PR back to level 0.

The expected check runs are a condition of the promotion, not of the level (``check_outcome``).

A level asks for as many independent reviewers (#111): one at level 1, two at level 2. Two
reviewers are distinct only if both their GitHub identity and their tuple ``(kind, model, effort,
review_args)`` differ; keys and descriptions do not count. At level 2 a reviewer is independent
of who wrote the code (the implementer, every fixer) only if both its identity and its ``(kind,
model, effort)`` differ from theirs. With ``provider_quorum`` (ADR 0005, P5-5, #409) that holds
at level 1 too, and at least one reviewer is of a provider other than every author's
(``config.provider_quorum``). With ``fallback_provider_ban`` (ADR 0005, P5-6, #410) a
reviewer's fallback model is never of an author's provider, at both levels and whatever the
quorum.
"""

import hashlib
import json
import posixpath
from dataclasses import dataclass

# Bumped when the meaning of a policy changes, so that receipts given under the old meaning no
# longer match.
POLICY_SCHEMA = 2  # 2: level 2 runs two independent reviewers (#111)
MIN_LEVEL, MAX_LEVEL = 1, 2
SENSITIVE_LEVEL = 2
ZERO_LEVEL = 0
# Never in the level-0 allowlist, even when listed (#107): devloop's prompts, the standards, CI
# workflows and schemas (the replay gate baseline among them), besides the sensitive paths; and
# agent instructions at any depth, compared without case.
ZERO_EXCLUDED = ('devloop/prompts/', 'standards/', '.github/', 'schemas/')
ZERO_EXCLUDED_NAMES = ('agents.md', 'claude.md')
# Markdown files are in the allowlist only when listed by their exact path.
MARKDOWN = ('.md', '.markdown', '.mdx')
# What a ``policy`` marker records: a PR stopped at a level devloop cannot review yet, an
# approval whose receipt no longer matches, or a promotion.
EVENTS = ('level', 'stale', 'promoted')
# The receipt fields an approval must match. ``reasons`` explains the level and is not compared.
# ``profiles`` are the identity and tuple each required reviewer reviews with: a reviewer
# outside the configured pool (level 1 keeps the PR's recorded one) is not covered by the policy
# version, and a change to its model, effort or arguments must still void its approvals.
BINDING = ('pr', 'base', 'head', 'policy', 'level', 'reviewers', 'profiles')
MAX_LISTED_PATHS = 50
# When the second reviewer of a round with two reviewers starts (#521): together with the first
# (the default, as before), only after the first approved, or together and stopped as soon as
# the first asks for changes.
PARALLEL, SEQUENTIAL, PARALLEL_CANCEL = 'parallel', 'sequential', 'parallel_cancel'
SECOND_REVIEWER_MODES = (PARALLEL, SEQUENTIAL, PARALLEL_CANCEL)


def _norm(entry):
    return entry.strip().strip('/')


@dataclass(frozen=True)
class ReviewPolicy:
    default_level: int = 1
    label: str = 'reviews:2'
    # Directory prefixes (``devloop/``) or file names (``AGENTS.md``), relative to the root.
    # None by default: they are restrictions of the managed repository, which its project file
    # adds (ADR 0006, R3; ssdlc's in ops/host/ssdlc/project.toml).
    sensitive_paths: tuple = ()
    # Files listed by ``sensitive_path_lists``, read when the configuration is loaded.
    sensitive_files: tuple = ()
    # The configured reviewer pool, in order: ``(key, app slug or None, kind, model, effort,
    # review_args[, fallback models[, review commands[, providers]]])`` each, the fallback
    # models as ``(model, effort)`` pairs (#121), the review commands the command prefixes an
    # agy reviewer may run (#122), the providers those of the model and of each fallback as
    # declared, '' each when not (ADR 0005, P5-5, #409). A change to any of them changes the
    # version, and so every receipt.
    reviewers: tuple = ()
    # Level 0 (#107): the label a trusted user adds, the allowlist (directory prefixes or files,
    # as ``sensitive_paths``) and the expected check runs, ``(name, app_id)`` each. Without an
    # allowlist or without expected checks level 0 is off.
    zero_label: str = 'reviews:0'
    zero_allowlist: tuple = ()
    zero_checks: tuple = ()
    # The agent of the advisory review of a PR in agent:needs-operator (#369), '' when off. It
    # never counts for the quorum, so it stays out of the version: turning it on or off voids
    # no approval.
    advisory_reviewer: str = ''
    # The findings-only reviewer (ADR 0005, D8, integration of 2026-11-19, #416), '' when off
    # (D10): at level 2, on a PR whose implementer signs with the implementer app and is the same
    # agent, it reviews each round besides the quorum, with another model. Its findings go to the
    # fix and the quorum closes them; its approval never counts and it never takes the place of a
    # reviewer of the quorum. It stays out of the version, as the advisory review: turning it on
    # or off voids no approval.
    findings_reviewer: str = ''
    # The SHA-256 of the project file on the host (ADR 0006, R2), '' without one. A change of
    # the file changes the version, and so voids every approval, as a change of the
    # configuration does.
    project: str = ''
    # The reviewer rules of ADR 0005, P5-5 (#409), off by default (D10): every agent declares
    # its provider; a reviewer is independent by tuple of every author at both levels, and the
    # quorum rule (ii) of D8 replaces the exclusion of the same agent; a reviewer whose chain is
    # all remembered unavailable is skipped when a new round chooses its reviewers. Off, the
    # reviewers are chosen as before (``config.same_agent``).
    provider_quorum: bool = False
    # The ban of ADR 0005, P5-6 (#410), off by default (D10): at both levels, whatever the
    # quorum rule, a reviewer's fallback model is never of the provider of an author of the PR,
    # and a reviewer of the pool whose fallback is of the provider of an agent that may implement,
    # other than its own model's, stops devloop at startup. Every provider is then declared.
    fallback_provider_ban: bool = False
    # When the second reviewer of a round with two reviewers starts (#521): ``parallel`` (the
    # default, as before), ``sequential`` or ``parallel_cancel``. Other than ``parallel`` it is
    # recorded in the receipt of each such round (``second_reviewer``), where the round reads
    # it: a request for changes, or for a decision, of the first reviewer closes the round.
    second_reviewer: str = PARALLEL

    def zero_enabled(self):
        return bool(self.zero_allowlist) and bool(self.zero_checks)

    def version(self):
        """Deterministic version of the loaded policy: the SHA-256 of its canonical form. Level
        0 enters it only when enabled, so a policy without it keeps the version of part 1; the
        project file (ADR 0006, R2) only when there is one, so that without it the version is
        the one before."""
        data = {'schema': POLICY_SCHEMA, 'default_level': self.default_level,
                'label': self.label, 'sensitive_level': SENSITIVE_LEVEL,
                'sensitive_paths': sorted({_norm(p) for p in self.sensitive_paths}),
                'sensitive_files': sorted({_norm(p) for p in self.sensitive_files}),
                # A reviewer's fallback models, (model, effort) each, count only when it has
                # some: without them the version is the one before #121.
                # Its review commands (agy, #122) likewise count only when it has some, tagged
                # so that they never read as fallback models.
                # Its declared providers (#409) likewise count only when it declares some,
                # tagged in the same way.
                'reviewers': [[*r[:5], list(r[5]), *([[list(f) for f in r[6]]]
                                                     if len(r) > 6 and r[6] else []),
                               *([{'commands': list(r[7])}] if len(r) > 7 and r[7] else []),
                               *([{'providers': list(r[8])}] if len(r) > 8 and any(r[8])
                                 else [])]
                              for r in self.reviewers]}
        # The reviewer rules of P5-5 (#409) only when on, so that off the version is the one
        # before.
        if self.provider_quorum:
            data['provider_quorum'] = True
        # The ban of P5-6 (#410) likewise: on, an approval a fallback of an author's provider
        # may have given no longer holds.
        if self.fallback_provider_ban:
            data['fallback_provider_ban'] = True
        # The mode of the second reviewer (#521) only when not the default, so that with it the
        # version is the one before.
        if self.second_reviewer != PARALLEL:
            data['second_reviewer'] = self.second_reviewer
        if self.zero_enabled():
            data['zero'] = {'label': self.zero_label,
                            'allowlist': sorted({_norm(p) for p in self.zero_allowlist}),
                            'checks': sorted([name, app] for name, app in self.zero_checks),
                            'excluded': sorted(ZERO_EXCLUDED + ZERO_EXCLUDED_NAMES),
                            'markdown': sorted(MARKDOWN)}
        if self.project:
            data['project'] = self.project
        text = json.dumps(data, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(text.encode()).hexdigest()

    def _entries(self):
        return [e for e in (_norm(p) for p in self.sensitive_paths + self.sensitive_files) if e]

    def covers(self, path, ancestors=False):
        """Whether ``path`` is, or is under, a sensitive entry. With ``ancestors`` a directory
        that contains one counts too (a symlink to it reaches the sensitive files)."""
        path = _norm(path)
        for entry in self._entries():
            if path == entry or path.startswith(entry + '/'):
                return True
            if ancestors and (path in ('', '.') or entry.startswith(path + '/')):
                return True
        return False


def link_target(path, target):
    """The repository path a symlink at ``path`` pointing to ``target`` resolves to, or None
    when it points outside the repository."""
    if not target or target.startswith('/'):
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved == '..' or resolved.startswith('../'):
        return None
    return resolved


def sensitive_changes(policy, changes):
    """The changed paths that impose the sensitive level, sorted.

    ``changes`` are ``{path, links}`` entries, one per path of the diff: deleted, added and
    modified files, both sides of a rename. ``links`` holds the targets of the path when it is
    a symlink on either side; a symlink counts also for what it points to, and a target outside
    the repository always counts."""
    hits = set()
    for change in changes:
        path = change['path']
        if policy.covers(path):
            hits.add(path)
        for target in change.get('links') or ():
            resolved = link_target(path, target)
            if resolved is None or policy.covers(resolved, ancestors=True):
                hits.add(path)
    return sorted(hits)


def zero_allowed(policy, change):
    """Whether a change of the diff (``{path, links}``) may be part of a level-0 PR: never a
    symlink, an excluded or sensitive path, nor a Markdown file that the allowlist does not
    list by its exact path.

    The path is matched against the allowlist exactly as Git names it: only the configured
    entries are normalized, so that a name with leading or trailing spaces is a different file
    and never matches an entry. Exclusions, and the Markdown rule, are checked on both the exact
    and the trimmed name, so that trimming can only refuse more."""
    path = change['path']
    if change.get('links') or not path or not _norm(path):
        return False
    for form in {path, _norm(path)}:
        low = form.lower()
        if posixpath.basename(low) in ZERO_EXCLUDED_NAMES or policy.covers(form):
            return False
        for entry in (_norm(e).lower() for e in ZERO_EXCLUDED):
            if low == entry or low.startswith(entry + '/'):
                return False
    entries = [e for e in (_norm(e) for e in policy.zero_allowlist) if e]
    if any(form.lower().endswith(MARKDOWN) for form in (path, _norm(path))):
        return path in entries
    return any(path == e or path.startswith(e + '/') for e in entries)


def outside_allowlist(policy, changes):
    """The changed paths that keep a PR out of level 0, sorted."""
    return sorted({c['path'] for c in changes if not zero_allowed(policy, c)})


def zero_grant(events, trusted_users):
    """``{by, at}`` of the label event that asks for level 0, or None: the latest ``labeled``
    or ``unlabeled`` event of the label must add it, and a trusted user must have written it.
    ``events`` are ``{event, by, at}``, oldest first."""
    last = events[-1] if events else None
    if (isinstance(last, dict) and last.get('event') == 'labeled'
            and last.get('by') in trusted_users and isinstance(last.get('at'), str)
            and last['at']):
        return {'by': last['by'], 'at': last['at']}
    return None


def zero_refused(records):
    """The time of the latest level-0 label event that a receipt among ``records`` refused, or
    None. That event, and any earlier one, no longer lowers the level."""
    times = [reason['at'] for receipt in map(receipt_of, records) if receipt
             for reason in receipt['reasons']
             if reason.get('kind') == 'zero' and reason.get('applied') is False
             and isinstance(reason.get('at'), str)]
    return max(times, default=None)


def evaluate(policy, *, pr, base, head, changes, labelers, trusted_users, previous_level,
             select, profile=lambda key: None, zero_events=(), refused=None):
    """The receipt of head ``head`` of PR ``pr`` against base commit ``base``.

    ``labelers`` are the logins of every ``labeled`` event of ``policy.label`` on the PR,
    oldest first; ``previous_level`` the highest level recorded on the PR so far (0 if none);
    ``select(level)`` the reviewers that level requires, in order: as many as the level, or
    none when the pool has not enough independent ones; ``profile(key)`` the identity and
    tuple reviewer ``key`` reviews with now. ``zero_events`` are the ``labeled`` and
    ``unlabeled`` events of ``policy.zero_label`` (see ``zero_grant``) and ``refused`` the
    ``zero_refused`` time of the PR's records; both count only when level 0 is enabled."""
    level = max(MIN_LEVEL, policy.default_level)
    reasons = [{'kind': 'default', 'level': level}]
    by = next((who for who in labelers if who in trusted_users), None)
    if by:
        level = max(level, SENSITIVE_LEVEL)
        reasons.append({'kind': 'label', 'level': SENSITIVE_LEVEL, 'label': policy.label,
                        'by': by})
    paths = sensitive_changes(policy, changes)
    if paths:
        level = max(level, SENSITIVE_LEVEL)
        reasons.append({'kind': 'paths', 'level': SENSITIVE_LEVEL,
                        'paths': paths[:MAX_LISTED_PATHS], 'count': len(paths)})
    if previous_level > level:
        level = previous_level
        reasons.append({'kind': 'previous', 'level': previous_level})
    level = min(level, MAX_LEVEL)
    grant = zero_grant(list(zero_events), trusted_users) if policy.zero_enabled() else None
    if grant:
        outside = outside_allowlist(policy, changes)
        why = [w for w, failed in (('level', level > MIN_LEVEL),
                                   ('refused', refused is not None and grant['at'] <= refused),
                                   ('allowlist', bool(outside))) if failed]
        reasons.append({'kind': 'zero', 'level': ZERO_LEVEL, 'label': policy.zero_label,
                        'by': grant['by'], 'at': grant['at'], 'applied': not why, 'why': why,
                        'outside': outside[:MAX_LISTED_PATHS], 'count': len(outside)})
        if not why:
            level = ZERO_LEVEL
    reviewers = list(select(level)) if level else []
    receipt = {'pr': pr, 'base': base, 'head': head, 'policy': policy.version(), 'level': level,
               'reasons': reasons, 'reviewers': reviewers,
               'profiles': [profile(key) for key in reviewers]}
    if policy.second_reviewer != PARALLEL and len(reviewers) > 1:
        # The round reads its mode here (#521): the policy version, which covers it, binds it.
        receipt['second_reviewer'] = policy.second_reviewer
    return receipt


def valid_profile(p):
    """An agent profile as markers record it: the GitHub ``identity`` (None for the ambient
    ``gh`` login), ``kind``, ``model``, ``effort`` and the CLI ``args`` of the turn."""
    return (isinstance(p, dict) and (p.get('identity') is None or isinstance(p['identity'], str))
            and all(isinstance(p.get(k), str) for k in ('kind', 'model', 'effort'))
            and isinstance(p.get('args'), list) and all(isinstance(a, str) for a in p['args']))


def agent_profile(agent, identity, mode):
    """The profile ``agent`` acts with in ``mode`` (``write`` or ``review``) under GitHub
    ``identity``, as markers and receipts record it. An agy profile also records the command
    prefixes its turns may run in ``mode`` (#122), passed with ``--settings``: a change to them
    voids the approvals given with the old ones, even for a reviewer outside the pool. The
    provider of the model, when the agent declares it (ADR 0005, P5-5, #409): the quorum rule
    of D8 reads it from the profiles of the authors and of the reviewers."""
    profile = {'identity': identity, 'kind': agent.kind, 'model': agent.model,
               'effort': agent.effort,
               'args': list(agent.review_args if mode == 'review' else agent.write_args)}
    if getattr(agent, 'provider', ''):
        profile['provider'] = agent.provider
    if agent.kind == 'agy':
        profile['commands'] = list(agent.review_commands if mode == 'review'
                                   else agent.write_commands)
    return profile


def profile_of(record):
    """The valid profile a PR, fix or review record carries, or None (records written before
    #111 carry none)."""
    profile = (record or {}).get('profile')
    return profile if valid_profile(profile) else None


def _model(p):
    return (p['kind'], p['model'], p['effort'])


def distinct(a, b):
    """Whether two reviewers count as two: different identity and different tuple."""
    return (a['identity'] != b['identity']
            and (*_model(a), tuple(a['args'])) != (*_model(b), tuple(b['args'])))


def independent(reviewer, author):
    """Whether ``reviewer`` may review code ``author`` wrote: different identity and different
    ``(kind, model, effort)``. Writing and reviewing arguments differ anyway, so they do not
    count."""
    return reviewer['identity'] != author['identity'] and _model(reviewer) != _model(author)


def another_model(reviewer, author):
    """Whether the findings-only reviewer (#416) may review code ``author`` wrote: independent
    of it (``independent``) and with a model of another name, whatever its effort. The issue
    asks for another model than the implementer's: the same model with another effort has the
    same blind spots."""
    return independent(reviewer, author) and reviewer['model'] != author['model']


def _is_int(value, low, high=None):
    return (isinstance(value, int) and not isinstance(value, bool) and value >= low
            and (high is None or value <= high))


def valid_receipt(r):
    return (isinstance(r, dict) and _is_int(r.get('pr'), 1)
            and all(isinstance(r.get(k), str) and r.get(k) for k in ('base', 'head', 'policy'))
            and _is_int(r.get('level'), ZERO_LEVEL, MAX_LEVEL)
            and isinstance(r.get('reviewers'), list)
            and all(isinstance(x, str) for x in r['reviewers'])
            and isinstance(r.get('reasons'), list)
            and all(isinstance(x, dict) for x in r['reasons'])
            # Receipts written before profiles were bound carry none: still valid, so their
            # level counts, but they never match a current receipt.
            and (r.get('profiles') is None or isinstance(r['profiles'], list)))


def receipt_of(record):
    """The valid receipt carried by a review or policy record, or None (a record written before
    receipts existed has none)."""
    receipt = (record or {}).get('receipt')
    return receipt if valid_receipt(receipt) else None


def recorded_level(records):
    """The highest level any valid receipt among ``records`` required, 0 if none."""
    return max((r['level'] for r in map(receipt_of, records) if r), default=0)


def differences(recorded, current):
    """The binding fields in which ``recorded`` differs from ``current``; all of them when
    there is no recorded receipt."""
    if recorded is None:
        return list(BINDING)
    return [k for k in BINDING if recorded.get(k) != current.get(k)]


def describe(receipt):
    """Markdown lines explaining a receipt's level."""
    lines = [f'- Livello effettivo: **{receipt["level"]}** (policy `{receipt["policy"][:12]}`, '
             f'base `{receipt["base"][:12]}`, head `{receipt["head"][:12]}`).']
    for reason in receipt['reasons']:
        kind = reason.get('kind')
        if kind == 'label':
            lines.append(f'- Label `{reason.get("label")}` messa da `{reason.get("by")}`.')
        elif kind == 'paths':
            paths = reason.get('paths') or []
            more = (reason.get('count') or len(paths)) - len(paths)
            lines.append('- Percorsi sensibili nel diff: '
                         + ', '.join(f'`{p}`' for p in paths)
                         + (f' e altri {more}' if more > 0 else '') + '.')
        elif kind == 'previous':
            lines.append(f'- Livello {reason.get("level")} già richiesto in precedenza su questa '
                         'PR: il livello non scende.')
        elif kind == 'zero':
            lines.append(_describe_zero(reason))
    reviewers = (', '.join(f'`{r}`' for r in receipt['reviewers'])
                 or ('nessuno (livello 0)' if receipt['level'] == ZERO_LEVEL
                     else 'nessun agent indipendente'))
    lines.append(f'- Reviewer richiesti: {reviewers}.')
    mode = receipt.get('second_reviewer')
    if mode in (SEQUENTIAL, PARALLEL_CANCEL):
        lines.append(f'- Secondo reviewer: `{mode}` ('
                     + ('parte solo se il primo approva' if mode == SEQUENTIAL else
                        'parte insieme al primo ed è annullato se il primo non approva')
                     + ').')
    # The findings-only reviewer (#416): outside the binding fields, never required.
    findings = receipt.get('findings')
    if isinstance(findings, list) and findings:
        lines.append('- Reviewer di soli finding, che non conta per il quorum: '
                     + ', '.join(f'`{r}`' for r in findings) + '.')
    return '\n'.join(lines)


_ZERO_WHY = {'level': 'il livello calcolato è 2, e la label deroga solo al livello 1',
             'refused': 'un ricalcolo precedente ha già escluso questa label: per tornare al '
                        'livello 0 serve una nuova label di un utente fidato'}


def _describe_zero(reason):
    text = (f'- Label `{reason.get("label")}` messa da `{reason.get("by")}` il '
            f'`{reason.get("at")}`')
    if reason.get('applied'):
        return text + ': livello 0, il diff è interamente nell\'allowlist.'
    why = [_ZERO_WHY[w] for w in reason.get('why') or () if w in _ZERO_WHY]
    if 'allowlist' in (reason.get('why') or ()):
        paths = reason.get('outside') or []
        more = (reason.get('count') or len(paths)) - len(paths)
        why.append('il diff esce dall\'allowlist del livello 0: '
                   + ', '.join(f'`{p}`' for p in paths) + (f' e altri {more}' if more > 0 else ''))
    return text + ', non applicata: ' + '; '.join(why) + '.'


def check_states(expected, runs):
    """The state of each expected check run on a head: ``{name, app_id, state}``, where state
    is ``missing``, ``pending`` or the conclusion of the most recent run (highest id) with that
    exact name and app. Commit statuses never count."""
    states = []
    for name, app_id in expected:
        mine = [r for r in runs if r.get('name') == name and r.get('app_id') == app_id]
        if not mine:
            state = 'missing'
        else:
            latest = max(mine, key=lambda r: r.get('id') or 0)
            state = ((latest.get('conclusion') or 'unknown')
                     if latest.get('status') == 'completed' else 'pending')
        states.append({'name': name, 'app_id': app_id, 'state': state})
    return states


def check_outcome(states):
    """``success`` when every expected check succeeded; ``failed`` when one is missing or
    ended otherwise (skipped, neutral, cancelled and a filtered workflow are no success);
    otherwise ``pending``. No expected check at all is never a success."""
    if not states or any(s['state'] not in ('success', 'pending') for s in states):
        return 'failed'
    return 'pending' if any(s['state'] == 'pending' for s in states) else 'success'


def changes_requested(reviews, trusted_users):
    """The trusted users whose native ``REQUEST_CHANGES`` still stands, sorted. A later
    approval of the same user withdraws it, and so does a dismissal (the review becomes
    ``DISMISSED``); a comment or a new commit does not. ``reviews`` are ``{login, state}``,
    oldest first."""
    standing = set()
    for review in reviews:
        who = review.get('login')
        if who not in trusted_users:
            continue
        if review.get('state') == 'APPROVED':
            standing.discard(who)
        elif review.get('state') == 'CHANGES_REQUESTED':
            standing.add(who)
    return sorted(standing)
