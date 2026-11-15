"""Deterministic, advisory impact report for a task plan, plus feedback metrics.

The report reads the repository at the plan's ``base_commit`` from Git objects:
the working tree, untracked files and symlinks are never consulted, and no
repository code is executed. It is evidence for the plan reviewer, not an
approval input: possible omissions never block a plan or change its state.

Analysis perimeter:

- static Python imports (``import``/``from``, relative imports, literal
  ``importlib.import_module``/``__import__`` names and literal ``mock.patch``
  targets), resolved against the repository root and the importing file's own
  directory;
- literal repository-relative path references in text files;
- owners from ``CODEOWNERS`` when it exists (otherwise absent);
- schema links only when a file literally names the other one.

Everything else is ``UNKNOWN``: other languages, unparsable Python and dynamic
imports whose destination is not a literal. A dynamic import is reported for
the file that contains it; no destination is inferred.
"""
import ast
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from ssdlc_tasks import TaskError, digest, file_hash


SCHEMA_VERSION = 1
KIND = 'ssdlc-impact-report'
EXCLUDED_PARTS = {'.git', '.ssdlc', '.tools', '__pycache__'}
MAX_SCANNED_BYTES = 1 << 20
CODEOWNERS = ('.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS')
DYNAMIC_MODULE_CALLS = {'__import__', 'import_module', 'run_module'}
DYNAMIC_PATH_CALLS = {'run_path', 'spec_from_file_location', 'load_source', 'SourceFileLoader'}
DELEGATED_CONTRACT = 'feedback-scope-delegation-v1'


class ImpactError(TaskError):
    pass


def _git_bytes(repo, *args, data=None):
    # Object reads only; never refresh the index or run repository hooks/monitors.
    proc = subprocess.run(['git', '-C', str(repo), '-c', 'core.fsmonitor=false', *args],
                          input=data, capture_output=True)
    if proc.returncode:
        raise ImpactError('Impact report requires the plan base commit in the target repository')
    return proc.stdout


def _tree(repo, commit):
    """Regular blobs at ``commit``; symlinks and submodules are not followed."""
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise ImpactError('Impact report requires an exact plan base commit')
    raw = _git_bytes(repo, 'ls-tree', '-r', '-l', '-z', '--full-tree', commit)
    entries = []
    for item in raw.split(b'\0'):
        if not item:
            continue
        meta, _, name = item.partition(b'\t')
        mode, kind, sha, size = meta.split()
        try:
            path = name.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            continue
        if any(part in EXCLUDED_PARTS for part in PurePosixPath(path).parts):
            continue
        entries.append((path, sha.decode(), int(size)))
    return sorted(entries)


def _contents(repo, entries):
    wanted = [(path, sha) for path, sha, size in entries if size <= MAX_SCANNED_BYTES]
    if not wanted:
        return {}
    out = _git_bytes(repo, 'cat-file', '--batch', data=''.join(sha + '\n' for _, sha in wanted).encode())
    result, offset = {}, 0
    for path, _ in wanted:
        end = out.index(b'\n', offset)
        size = int(out[offset:end].split()[2])
        result[path] = out[end + 1:end + 1 + size]
        offset = end + 2 + size
    return result


def _module_names(path):
    if not path.endswith('.py'):
        return []
    parts = list(PurePosixPath(path[:-3]).parts)
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return ['.'.join(parts)] if parts else []


def _is_test(path):
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (any(p in {'tests', 'test', 'fixtures', 'fixture'} for p in parts[:-1])
            or name.startswith('test_') or name.endswith('_test.py') or name == 'conftest.py')


def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _call_name(node):
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _python_references(path, source):
    """Return (dotted references with line and kind, unknown rows)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], [dict(path=path, line=exc.lineno or 0, reason='PARSE_ERROR')]
    except ValueError:
        return [], [dict(path=path, line=0, reason='PARSE_ERROR')]
    package = list(PurePosixPath(path).parts[:-1])
    refs, unknown = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs.extend((alias.name, node.lineno, 'IMPORT', False) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = node.level - 1
                if up > len(package):
                    unknown.append(dict(path=path, line=node.lineno, reason='UNRESOLVED_RELATIVE_IMPORT'))
                    continue
                base = package[:len(package) - up]
                module = '.'.join(base + (node.module.split('.') if node.module else []))
                # Relative imports are package-relative only, never sys.path roots.
                rooted = True
            else:
                module, rooted = node.module, False
            if module:
                refs.append((module, node.lineno, 'IMPORT', rooted))
            refs.extend(((module + '.' if module else '') + alias.name, node.lineno, 'IMPORT', rooted)
                        for alias in node.names if alias.name != '*')
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            first = _literal(node.args[0]) if node.args else None
            if name in DYNAMIC_MODULE_CALLS:
                if first is None or first.startswith('.'):
                    unknown.append(dict(path=path, line=node.lineno, reason='DYNAMIC_IMPORT'))
                else:
                    refs.append((first, node.lineno, 'DYNAMIC_IMPORT_LITERAL', False))
            elif name in DYNAMIC_PATH_CALLS:
                # A literal location is covered by the literal path scan.
                if not node.args or any(_literal(arg) is None for arg in node.args[:2]):
                    unknown.append(dict(path=path, line=node.lineno, reason='DYNAMIC_IMPORT'))
            elif name == 'patch' and node.args:
                if first is None:
                    unknown.append(dict(path=path, line=node.lineno, reason='DYNAMIC_PATCH_TARGET'))
                else:
                    refs.append((first, node.lineno, 'PATCH_TARGET', False))
    return refs, unknown


def _resolve(modules, importer, dotted, kind, rooted):
    """Exact module paths for a dotted reference; no guessing beyond the roots."""
    roots = [''] if rooted else ['', '.'.join(PurePosixPath(importer).parts[:-1])]
    found = set()
    for root in dict.fromkeys(roots):
        full = (root + '.' + dotted) if root else dotted
        if kind == 'PATCH_TARGET':
            # ``patch('pkg.mod.attr')`` imports the longest module prefix.
            parts = full.split('.')
            for size in range(len(parts) - 1, 0, -1):
                if '.'.join(parts[:size]) in modules:
                    found.add(modules['.'.join(parts[:size])])
                    break
        elif full in modules:
            found.add(modules[full])
    return found


def _path_pattern(paths):
    if not paths:
        return None
    alternatives = '|'.join(re.escape(p) for p in sorted(paths, key=lambda p: (-len(p), p)))
    return re.compile(r'(?<![\w./-])(?:' + alternatives + r')(?![\w-]|\.\w)')


def _first_lines(pattern, text):
    result = {}
    if pattern is None:
        return result
    for match in pattern.finditer(text):
        if match.group(0) not in result:
            result[match.group(0)] = text.count('\n', 0, match.start()) + 1
    return result


def _codeowners(files, texts, targets):
    source = next((name for name in CODEOWNERS if name in files), None)
    if source is None:
        return None, {t: dict(status='ABSENT') for t in targets}
    text = texts.get(source)
    if text is None:
        return source, {t: dict(status='UNKNOWN', reason='UNREADABLE_CODEOWNERS') for t in targets}
    rules = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        pattern, *owners = line.split()
        if pattern.startswith('!') or any(c in pattern for c in '[]\\'):
            return source, {t: dict(status='UNKNOWN', reason='UNSUPPORTED_CODEOWNERS_PATTERN') for t in targets}
        rules.append((_owner_regex(pattern), sorted(owners), number))
    result = {}
    for target in targets:
        match = None
        for regex, owners, number in rules:
            if regex.match(target):
                match = (owners, number)
        if match is None or not match[0]:
            result[target] = dict(status='NONE', evidence_path=source,
                                  line=match[1] if match else None)
        else:
            result[target] = dict(status='DECLARED', owners=match[0], evidence_path=source, line=match[1])
    return source, result


def _owner_regex(pattern):
    """The gitignore subset GitHub documents for CODEOWNERS."""
    directory = pattern.endswith('/')
    body = pattern.strip('/')
    anchored = pattern.startswith('/') or '/' in body
    out, i = '', 0
    while i < len(body):
        if body.startswith('**/', i):
            out += '(?:.*/)?'; i += 3
        elif body.startswith('**', i):
            out += '.*'; i += 2
        elif body[i] == '*':
            out += '[^/]*'; i += 1
        elif body[i] == '?':
            out += '[^/]'; i += 1
        else:
            out += re.escape(body[i]); i += 1
    prefix = '^' if anchored else '^(?:.*/)?'
    # ``docs/*`` owns direct children only; other patterns also own subtrees.
    suffix = '/.*$' if directory else '$' if body.endswith('/*') else '(?:/.*)?$'
    return re.compile(prefix + out + suffix)


def build_report(repo, plan):
    """Advisory impact report of every plan ``target_file`` at the plan base."""
    targets = sorted({p for task in plan['tasks'] for p in task['target_files']})
    entries = _tree(repo, plan['base_commit'])
    files = {path for path, _, _ in entries}
    raw = _contents(repo, entries)
    texts, unknown, unscanned = {}, [], []
    for path, _, size in entries:
        if size > MAX_SCANNED_BYTES:
            unscanned.append(dict(path=path, reason='TOO_LARGE'))
            continue
        try:
            texts[path] = raw[path].decode('utf-8')
        except UnicodeDecodeError:
            if path.endswith('.py'):
                unknown.append(dict(path=path, line=0, reason='UNREADABLE'))
    modules = {}
    for path in sorted(files | set(targets)):
        for name in _module_names(path):
            modules.setdefault(name, path)
    relations = {t: dict(consumers=set(), tests=set(), validators=set()) for t in targets}
    target_set = set(targets)
    for path in sorted(texts):
        if not path.endswith('.py'):
            continue
        refs, problems = _python_references(path, texts[path])
        unknown.extend(problems)
        seen = set()
        for dotted, line, kind, rooted in sorted(refs, key=lambda r: (r[1], r[0], r[2])):
            for found in _resolve(modules, path, dotted, kind, rooted):
                if found not in target_set or found == path or (found, kind) in seen:
                    continue
                seen.add((found, kind))
                group = 'tests' if _is_test(path) else 'consumers'
                relations[found][group].add((path, path, line, kind))
    by_target = _path_pattern(target_set)
    schemas = sorted(p for p in files if p.startswith('schemas/'))
    by_schema = _path_pattern(set(schemas))
    for path in sorted(texts):
        if _is_test(path) or path.startswith('schemas/'):
            group = 'tests' if _is_test(path) else 'validators'
            for target, line in _first_lines(by_target, texts[path]).items():
                if target != path:
                    relations[target][group].add((path, path, line, 'PATH_REFERENCE'))
        if path in target_set:
            for schema, line in _first_lines(by_schema, texts[path]).items():
                if schema != path:
                    relations[path]['validators'].add((schema, path, line, 'PATH_REFERENCE'))
    owners_source, owners = _codeowners(files, texts, targets)
    rows, omissions = [], {}
    for target in targets:
        row = dict(path=target, exists_at_base=target in files,
                   consumers_status='STATIC_PYTHON' if target.endswith('.py') else 'UNKNOWN',
                   owners=owners[target])
        for group in ('consumers', 'tests', 'validators'):
            row[group] = [dict(path=p, evidence_path=e, line=n, via=v)
                          for p, e, n, v in sorted(relations[target][group])]
        rows.append(row)
        for group, label in (('consumers', 'CONSUMER'), ('tests', 'TEST')):
            for item in row[group]:
                if item['path'] not in target_set:
                    entry = omissions.setdefault(item['path'], dict(relations=set(), targets=set()))
                    entry['relations'].add(label)
                    entry['targets'].add(target)
    return dict(schema_version=SCHEMA_VERSION, kind=KIND, advisory=True, blocking=False,
                complete_dependency_proof=False, base_commit=plan['base_commit'], plan_sha256=digest(plan),
                target_files=targets, owners_source=owners_source, targets=rows,
                possible_omissions=[dict(path=p, relations=sorted(v['relations']), targets=sorted(v['targets']))
                                    for p, v in sorted(omissions.items())],
                unknown=sorted(unknown, key=lambda u: (u['path'], u['line'], u['reason'])),
                unscanned=unscanned)


def related_paths(report):
    """Paths a report relates to its targets; reads legacy ``fixture_impact`` too."""
    if isinstance(report, dict) and report.get('kind') == KIND:
        if not isinstance(report.get('targets'), list):
            raise TaskError('Impact report structure changed')
        return {item['path'] for row in report['targets']
                for group in ('consumers', 'tests', 'validators') for item in row[group]}
    if not isinstance(report, dict) or not isinstance(report.get('references'), list):
        raise TaskError('Impact report structure changed')
    return {row['path'] for row in report['references']}


# ---------------------------------------------------------------------------
# Additive feedback-summary metrics. Missing evidence stays UNKNOWN, never 0.
# ---------------------------------------------------------------------------

def profile_delta(old, new):
    """Changed limit fields, in the ``operator_recovery`` delta format."""
    return {key: dict(old=old.get(key), new=new.get(key))
            for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)}


def pending_touched(journal_state, plan, scope_diff):
    """IDs of PENDING tasks whose pre-extension targets meet the added paths."""
    added = {p for change in scope_diff.values() for p in change.get('added', [])}
    touched = []
    for task in plan['tasks']:
        tid = task['task_id']
        change = scope_diff.get(tid, {})
        before = (set(task['target_files']) - set(change.get('added', []))) | set(change.get('removed', []))
        if journal_state['tasks'].get(tid, {}).get('status') == 'PENDING' and before & added:
            touched.append(tid)
    return sorted(touched)


def _read_json(path):
    if path.is_symlink() or not path.is_file():
        raise TaskError('Evidence is missing or not a regular file')
    return json.loads(path.read_text())


def _transitions(ticket):
    """Validated execution generations, oldest first, from the active selector."""
    from ssdlc_transitions import TransitionStore
    store_root = ticket / 'execution'
    if not (store_root / 'active.json').exists():
        return []
    store, chain, seen = TransitionStore(store_root), [], set()
    pointer = _read_json(store_root / 'active.json')
    while isinstance(pointer, dict) and (store_root / str(pointer.get('transition_id', ''))).is_dir():
        identity = pointer['transition_id']
        if identity in seen:
            raise TaskError('Execution lineage contains a cycle')
        seen.add(identity)
        store.load_pointer(pointer)
        generation = store_root / identity
        intent = _read_json(generation / 'intent.json')
        recipe = _read_json(generation / 'recipe.json') if (generation / 'recipe.json').exists() else None
        chain.append(dict(transition_id=identity, proposal=intent['proposal'], recipe=recipe))
        pointer = intent['proposal'].get('parent_selector')
    return chain[::-1]


def _manual_extensions(ticket, chain):
    rows = []
    for item in chain:
        proposal, recipe = item['proposal'], item['recipe']
        scope = proposal.get('scope_diff') or {}
        added = sorted({p for change in scope.values() for p in change.get('added', [])})
        if proposal.get('authorization_contract') == DELEGATED_CONTRACT or not added:
            continue
        row = dict(transition_id=item['transition_id'], authorization_contract=proposal.get('authorization_contract'),
                   added_paths=added, pending_task_ids=None, status='UNKNOWN')
        try:
            plan = _read_json(ticket / 'execution-runtimes' / item['transition_id'] / '02-design/plan.json')
            if recipe is None or digest(plan) != recipe['subject']['new_plan_sha256']:
                raise TaskError('Extended plan differs from its sealed subject')
            row.update(pending_task_ids=pending_touched(recipe['journal_state'], plan, scope), status='MEASURED')
        except (TaskError, OSError, ValueError, KeyError, TypeError):
            pass
        rows.append(row)
    return rows


def _registered(ticket, ref):
    state = _read_json(ticket / 'state.json')
    meta = state.get('artifacts', {}).get(ref)
    path = ticket / ref
    if meta is None or path.is_symlink() or not path.is_file() or file_hash(path) != meta['sha256']:
        return None
    return json.loads(path.read_text())


def _avoidable(ticket, extensions):
    plan = _registered(ticket, '02-design/plan.json')
    if plan is None:
        return dict(plan_sha256=None, report_sha256=None, status='UNKNOWN', count=None, paths=[],
                    reason='NO_REGISTERED_PLAN')
    plan_sha = digest(plan)
    report = _registered(ticket, '02-design/task-planning/impact-report-' + plan_sha + '.json')
    if not isinstance(report, dict) or report.get('kind') != KIND or report.get('plan_sha256') != plan_sha:
        return dict(plan_sha256=plan_sha, report_sha256=None, status='UNKNOWN', count=None, paths=[],
                    reason='NO_IMPACT_REPORT')
    observed = {}
    if (ticket / 'feedback/authority.json').exists():
        from ssdlc_feedback import FeedbackStore
        for row in FeedbackStore(ticket).inspect()['requests'].values():
            if row['request']['cause'] == 'MISSING_TARGET':
                for path in row['request']['requested_files']:
                    observed.setdefault(path, set()).add('FEEDBACK_MISSING_TARGET')
    for row in extensions:
        for path in row['added_paths']:
            observed.setdefault(path, set()).add('MANUAL_EXTENSION')
    flagged = {row['path'] for row in report['possible_omissions']}
    paths = [dict(path=p, observed_in=sorted(observed[p])) for p in sorted(flagged & set(observed))]
    return dict(plan_sha256=plan_sha, report_sha256=digest(report), status='MEASURED', count=len(paths), paths=paths)


def _profiles(ticket, chain):
    """The ticket's own frozen limits, else those of its oldest generation."""
    from ssdlc_recovery import LimitsStore
    from ssdlc_transitions import runtime_root
    if (ticket / 'limits/policy.json').exists():
        first = ticket
    elif chain:
        first = ticket / 'execution-runtimes' / chain[0]['transition_id']
    else:
        return dict(status='UNKNOWN', reason='NO_AUTHORIZED_PROFILE')
    initial = LimitsStore(first)._load(repair=False)[0]['policy']
    active = LimitsStore(runtime_root(ticket))._load(repair=False)[0]['policy']
    return dict(status='MEASURED', initial_profile_sha256=digest(initial), active_profile_sha256=digest(active),
                changes=profile_delta(initial, active))


def summary_metrics(ticket):
    """Three additive ``feedback-summary.json`` keys, computed from host evidence."""
    ticket = Path(ticket)
    errors = (TaskError, OSError, ValueError, KeyError, TypeError)
    try:
        chain = _transitions(ticket)
        extensions = _manual_extensions(ticket, chain)
        pending = dict(status='MEASURED', transitions=extensions)
    except errors:
        chain = extensions = None
        pending = dict(status='UNKNOWN', transitions=[], reason='EXECUTION_LINEAGE_UNAVAILABLE')
    if extensions is None:
        avoidable = dict(plan_sha256=None, report_sha256=None, status='UNKNOWN', count=None, paths=[],
                         reason='EXECUTION_LINEAGE_UNAVAILABLE')
        delta = dict(status='UNKNOWN', reason='EXECUTION_LINEAGE_UNAVAILABLE')
    else:
        try:
            avoidable = _avoidable(ticket, extensions)
        except errors:
            avoidable = dict(plan_sha256=None, report_sha256=None, status='UNKNOWN', count=None, paths=[],
                             reason='EVIDENCE_UNAVAILABLE')
        try:
            delta = _profiles(ticket, chain)
        except errors:
            delta = dict(status='UNKNOWN', reason='PROFILE_UNAVAILABLE')
    return dict(avoidable_omissions=[avoidable], profile_delta=delta, manual_extension_pending_tasks=pending)
