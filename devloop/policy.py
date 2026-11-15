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

Nothing lowers a level, and nothing takes it below 1.
"""

import hashlib
import json
import posixpath
from dataclasses import dataclass

# Bumped when the meaning of a policy changes, so that receipts given under the old meaning no
# longer match.
POLICY_SCHEMA = 1
MIN_LEVEL, MAX_LEVEL = 1, 2
SENSITIVE_LEVEL = 2
# What a ``policy`` marker records: a PR stopped at a level devloop cannot review yet, an
# approval whose receipt no longer matches, or a promotion.
EVENTS = ('level', 'stale', 'promoted')
# The receipt fields an approval must match. ``reasons`` explains the level and is not compared.
BINDING = ('pr', 'base', 'head', 'policy', 'level', 'reviewers')
MAX_LISTED_PATHS = 50


def _norm(entry):
    return entry.strip().strip('/')


@dataclass(frozen=True)
class ReviewPolicy:
    default_level: int = 1
    label: str = 'reviews:2'
    # Directory prefixes (``devloop/``) or file names (``AGENTS.md``), relative to the root.
    sensitive_paths: tuple = ('devloop/', 'AGENTS.md', 'CLAUDE.md', '.github/', 'schemas/')
    # Files listed by ``sensitive_path_lists``, read when the configuration is loaded.
    sensitive_files: tuple = ()

    def version(self):
        """Deterministic version of the loaded policy: the SHA-256 of its canonical form."""
        data = {'schema': POLICY_SCHEMA, 'default_level': self.default_level,
                'label': self.label, 'sensitive_level': SENSITIVE_LEVEL,
                'sensitive_paths': sorted({_norm(p) for p in self.sensitive_paths}),
                'sensitive_files': sorted({_norm(p) for p in self.sensitive_files})}
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


def evaluate(policy, *, pr, base, head, changes, labelers, trusted_users, previous_level,
             reviewer):
    """The receipt of head ``head`` of PR ``pr`` against base commit ``base``.

    ``labelers`` are the logins of every ``labeled`` event of ``policy.label`` on the PR,
    oldest first; ``previous_level`` the highest level recorded on the PR so far (0 if none);
    ``reviewer`` the agent that reviews at level 1 (None if there is none)."""
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
    # Level 2 does not start more reviewers yet (#111): it needs a human.
    reviewers = [reviewer] if level == 1 and reviewer else []
    return {'pr': pr, 'base': base, 'head': head, 'policy': policy.version(), 'level': level,
            'reasons': reasons, 'reviewers': reviewers}


def _is_int(value, low, high=None):
    return (isinstance(value, int) and not isinstance(value, bool) and value >= low
            and (high is None or value <= high))


def valid_receipt(r):
    return (isinstance(r, dict) and _is_int(r.get('pr'), 1)
            and all(isinstance(r.get(k), str) and r.get(k) for k in ('base', 'head', 'policy'))
            and _is_int(r.get('level'), MIN_LEVEL, MAX_LEVEL)
            and isinstance(r.get('reviewers'), list)
            and all(isinstance(x, str) for x in r['reviewers'])
            and isinstance(r.get('reasons'), list)
            and all(isinstance(x, dict) for x in r['reasons']))


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
    reviewers = ', '.join(f'`{r}`' for r in receipt['reviewers']) or 'nessun agent'
    lines.append(f'- Reviewer richiesti: {reviewers}.')
    return '\n'.join(lines)
