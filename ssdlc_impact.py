"""Deterministic, advisory impact report for a task plan, plus feedback metrics.

The report reads the repository at the plan's ``base_commit`` from Git objects:
the working tree, untracked files and symlinks are never consulted, and no
repository code is executed. It is evidence for the plan reviewer, not an
approval input: possible omissions never block a plan or change its state.

Analysis perimeter:

- static Python imports (``import``/``from``, relative imports, literal
  ``importlib.import_module``/``__import__`` names and literal ``mock.patch``
  targets). Relative imports resolve against the importer's package and
  absolute ones against the repository root only. An absolute import that the
  importer's own directory would resolve differently depends on ``sys.path``
  and is ``UNKNOWN`` for that line;
- literal repository-relative path references in text files;
- owners from ``CODEOWNERS`` when it exists (otherwise absent);
- schema links only when a file literally names the other one.

Everything else is ``UNKNOWN``: other languages, unparsable Python and dynamic
imports whose destination is not a literal. A dynamic import is reported for
the file that contains it; no destination is inferred.

When tasks declare ``contract_changes``, the report also verifies them
(``contracts``): an uncovered or unordered consumer whose incompatibility is
demonstrated (an unguarded use of a removed name) is a ``PLAN_DEFECT`` the plan
review must report. That is the only
part of the report that can require a plan revision; undecidable cases stay
non-blocking diagnoses.
"""
import ast
import builtins
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
                relative = True
            else:
                module, relative = node.module, False
            if module:
                refs.append((module, node.lineno, 'IMPORT', relative))
            refs.extend(((module + '.' if module else '') + alias.name, node.lineno, 'IMPORT', relative)
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


def _lookup(modules, full, kind):
    if kind == 'PATCH_TARGET':
        # ``patch('pkg.mod.attr')`` imports the longest module prefix.
        parts = full.split('.')
        for size in range(len(parts) - 1, 0, -1):
            if '.'.join(parts[:size]) in modules:
                return modules['.'.join(parts[:size])]
        return None
    return modules.get(full)


def _resolve(modules, importer, dotted, kind, relative):
    """Return (module path or None, environment-dependent) for a dotted reference.

    Relative imports are already anchored to the importer's package. Absolute
    imports resolve against the repository root only: the importer's own
    directory is a ``sys.path`` root only when that file runs as a script or
    the environment adds it, which is not statically provable. When that
    directory would yield a different module, the resolution is environment
    dependent and the caller records it as ``UNKNOWN``.
    """
    found = _lookup(modules, dotted, kind)
    directory = '.'.join(PurePosixPath(importer).parts[:-1])
    if relative or not directory:
        return found, False
    return found, _lookup(modules, directory + '.' + dotted, kind) not in (None, found)


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
        seen, ambiguous = set(), set()
        for dotted, line, kind, relative in sorted(refs, key=lambda r: (r[1], r[0], r[2])):
            found, environment_dependent = _resolve(modules, path, dotted, kind, relative)
            if environment_dependent and line not in ambiguous:
                # No destination is inferred from the importer's directory.
                ambiguous.add(line)
                unknown.append(dict(path=path, line=line, reason='ENVIRONMENT_DEPENDENT_IMPORT'))
            if found is None or found not in target_set or found == path or (found, kind) in seen:
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
    contracts = None
    if any(task.get('contract_changes') for task in plan['tasks']):
        contracts = _contract_analysis(plan, files, texts, modules)
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
    report = dict(schema_version=SCHEMA_VERSION, kind=KIND, advisory=True, blocking=False,
                  complete_dependency_proof=False, base_commit=plan['base_commit'], plan_sha256=digest(plan),
                  target_files=targets, owners_source=owners_source, targets=rows,
                  possible_omissions=[dict(path=p, relations=sorted(v['relations']), targets=sorted(v['targets']))
                                      for p, v in sorted(omissions.items())],
                  unknown=sorted(unknown, key=lambda u: (u['path'], u['line'], u['reason'])),
                  unscanned=unscanned)
    if contracts is not None:
        # Only plans that declare contract changes get the key: older reports keep their bytes.
        report['contracts'] = contracts
    return report


# ---------------------------------------------------------------------------
# Declared contract changes. A consumer is demonstrated only by a static use
# of the declared name at the plan base, through a binding whose identity is
# provable in its scope; everything else is a diagnosis. A use demonstrates an
# incompatibility only when removing the name makes it fail.
# ---------------------------------------------------------------------------

BREAKING_CHANGES = {'INCOMPATIBLE', 'REMOVED'}
OBLIGATION_FIELDS = ('requirement_refs', 'standard_refs', 'quality_check_refs')
_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_GUARD_CALLS = {'hasattr', 'getattr'}


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return base + '.' + node.attr if base else None
    return None


def _top_level_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split('.')[0] for a in node.names if a.name != '*')
    return names


def _names_producer(modules, importer, dotted, relative, producer):
    """True/False when ``dotted`` statically is/is not the producer; None when it depends on ``sys.path``."""
    found, environment_dependent = _resolve(modules, importer, dotted, 'IMPORT', relative)
    if environment_dependent:
        directory = '.'.join(PurePosixPath(importer).parts[:-1])
        if producer in (found, _lookup(modules, directory + '.' + dotted, 'IMPORT')):
            return None
    return found == producer


class _Binding:
    """One binding of a name: an import (``identity`` names the bound object) or anything else."""
    __slots__ = ('kind', 'bound', 'identity', 'hit')

    def __init__(self, kind, bound=None, identity=None, hit=False):
        self.kind, self.bound, self.identity, self.hit = kind, bound, identity, hit


_OTHER = _Binding('OTHER')
_STAR = _Binding('STAR')


class _Scope:
    __slots__ = ('kind', 'parent', 'module', 'raw', 'local', 'globals', 'nonlocals', 'star')

    def __init__(self, kind, parent):
        self.kind, self.parent = kind, parent
        self.module = parent.module if parent else self
        self.raw, self.local, self.globals, self.nonlocals, self.star = [], {}, set(), set(), False

    def owner(self, name):
        """The scope that a binding of ``name`` in this scope assigns."""
        if name in self.globals:
            return self.module
        if name not in self.nonlocals:
            return self
        scope = self.parent
        while scope.kind != 'module':
            if scope.kind != 'class' and name not in scope.nonlocals:
                if name in scope.globals:
                    return self.module
                if any(n == name for n, _ in scope.raw):
                    return scope
            scope = scope.parent
        return self.module

    def lookup(self, name, free=False):
        """Every binding ``name`` may denote here, flow-insensitively; empty for builtins and unbound names."""
        scope = self
        while True:
            if scope.kind == 'module':
                return scope.local.get(name, []) + ([_STAR] if scope.star else [])
            if name in scope.globals:
                scope = scope.module
                continue
            if scope.kind == 'class':
                if not free and name in scope.local:
                    # A class body reads its own binding or, before it, the enclosing one.
                    return scope.local[name] + scope.parent.lookup(name, True)
            elif name not in scope.nonlocals and name in scope.local:
                return list(scope.local[name])
            scope, free = scope.parent, True


def _guard_test(expr):
    """True when a condition checks for an attribute or for type checking."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and _call_name(node) in _GUARD_CALLS:
            return True
        if (isinstance(node, ast.Name) and node.id == 'TYPE_CHECKING'
                or isinstance(node, ast.Attribute) and node.attr == 'TYPE_CHECKING'):
            return True
    return False


def _scan_scopes(tree, on_import):
    """Bind every name to its scope; return the attribute and call sites with their scope and guard.

    A site is guarded when the removal of a name it uses may be handled or never
    evaluated: a ``try`` body with handlers, a ``hasattr``/``getattr`` or
    ``TYPE_CHECKING`` condition, ``suppress`` or an annotation.
    """
    module = _Scope('module', None)
    scopes, sites = [module], []
    stack = [(node, module, False) for node in reversed(tree.body)]

    def push(nodes, scope, guarded):
        stack.extend((n, scope, guarded) for n in reversed([n for n in nodes if n is not None]))

    def child(kind, scope):
        new = _Scope(kind, scope)
        scopes.append(new)
        return new

    def bind_arguments(args, scope, outer, guarded):
        for arg in (*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg):
            if arg is not None:
                scope.raw.append((arg.arg, _OTHER))
                push([arg.annotation], outer, True)
        push([*args.defaults, *args.kw_defaults], outer, guarded)

    while stack:
        node, scope, guarded = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                scope.raw.append((node.name, _OTHER))
                push(node.decorator_list, scope, guarded)
                push([node.returns], scope, True)
            inner = child('function', scope)
            bind_arguments(node.args, inner, scope, guarded)
            push(node.body if isinstance(node.body, list) else [node.body], inner, guarded)
        elif isinstance(node, ast.ClassDef):
            scope.raw.append((node.name, _OTHER))
            push([*node.decorator_list, *node.bases, *node.keywords], scope, guarded)
            push(node.body, child('class', scope), guarded)
        elif isinstance(node, _COMPREHENSIONS):
            inner = child('comprehension', scope)
            # The first iterable is evaluated in the enclosing scope.
            push([node.generators[0].iter], scope, guarded)
            for index, generator in enumerate(node.generators):
                push([generator.target, *generator.ifs, *([generator.iter] if index else [])], inner, guarded)
            push([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt], inner, guarded)
        elif isinstance(node, ast.NamedExpr):
            target = scope
            while target.kind == 'comprehension':
                target = target.parent
            target.raw.append((node.target.id, _OTHER))
            push([node.value], scope, guarded)
        elif isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                scope.raw.append((node.id, _OTHER))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, binding in on_import(node, guarded):
                if name == '*':
                    scope.star = True
                else:
                    scope.raw.append((name, binding))
        elif isinstance(node, ast.Global):
            scope.globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            scope.nonlocals.update(node.names)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                scope.raw.append((node.name, _OTHER))
            push([node.type, *node.body], scope, guarded)
        elif isinstance(node, (ast.Try, getattr(ast, 'TryStar', ast.Try))):
            push(node.body, scope, guarded or bool(node.handlers))
            push([*node.handlers, *node.orelse, *node.finalbody], scope, guarded)
        elif isinstance(node, (ast.If, ast.While, ast.IfExp)):
            branch = guarded or _guard_test(node.test)
            push([node.test], scope, guarded)
            push(node.body if isinstance(node.body, list) else [node.body], scope, branch)
            push(node.orelse if isinstance(node.orelse, list) else [node.orelse], scope, branch)
        elif isinstance(node, ast.BoolOp):
            push(node.values, scope, guarded or any(_guard_test(v) for v in node.values))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            push(node.items, scope, guarded)
            handled = any(isinstance(n, ast.Call) and _call_name(n) == 'suppress'
                          for item in node.items for n in ast.walk(item.context_expr))
            push(node.body, scope, guarded or handled)
        elif isinstance(node, ast.AnnAssign):
            push([node.target, node.value], scope, guarded)
            push([node.annotation], scope, True)
        else:
            if type(node).__name__.startswith('Match'):
                # Capture patterns of ``match`` bind names too.
                for field in ('name', 'rest'):
                    if isinstance(getattr(node, field, None), str):
                        scope.raw.append((getattr(node, field), _OTHER))
            if isinstance(node, (ast.Attribute, ast.Call)):
                sites.append((node, scope, guarded))
            push(list(ast.iter_child_nodes(node)), scope, guarded)
    for scope in scopes:
        for name, binding in scope.raw:
            scope.owner(name).local.setdefault(name, []).append(binding)
    return sites


def _contract_uses(path, tree, modules, producer, contract):
    """Uses of ``producer.contract`` in ``path``, the lines where that is not decidable and its recreations.

    Each use is ``(line, via, breaking)``: ``breaking`` when removing the name
    makes that use fail. A name is the producer only when every binding it may
    denote in its scope is the same import; otherwise the use is ``UNKNOWN``.
    The same holds for ``getattr`` (the builtin) and ``patch`` (``mock``'s).
    Each recreation is the line of a write that may give the producer the name
    back: an assignment, a ``setattr`` or a ``patch`` allowed to create it.
    After one, a read of the same file no longer demonstrates that the
    original attribute is required.
    """
    package = list(PurePosixPath(path).parts[:-1])
    targets = {name + '.' + contract for name in _module_names(producer)}
    patches = {'unittest.mock.patch'} | ({'mock.patch'} if 'mock' not in modules else set())
    objects = {p + '.object' for p in patches}
    uses, unknown, recreations = [], [], []
    # ``api.load += f`` reads the attribute before writing it.
    augmented = {id(n.target) for n in ast.walk(tree) if isinstance(n, ast.AugAssign)}

    def imported(node, guarded):
        bindings = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                hit = _names_producer(modules, path, alias.name, False, producer)
                if hit is None:
                    unknown.append((node.lineno, 'ENVIRONMENT_DEPENDENT_IMPORT'))
                if alias.asname:
                    bindings.append((alias.asname, _Binding('IMPORT', alias.asname, alias.name, hit)))
                else:
                    # ``import a.b`` binds ``a``; only ``a.b`` itself names the module.
                    head = alias.name.split('.')[0]
                    bindings.append((head, _Binding('IMPORT', alias.name, head, hit)))
            return bindings
        relative = bool(node.level)
        if relative:
            up = node.level - 1
            if up > len(package):
                return [(alias.asname or alias.name, _OTHER) for alias in node.names]
            module = '.'.join(package[:len(package) - up] + (node.module.split('.') if node.module else []))
        else:
            module = node.module
        hit = _names_producer(modules, path, module, relative, producer) if module else False
        if hit is None:
            unknown.append((node.lineno, 'ENVIRONMENT_DEPENDENT_IMPORT'))
        for alias in node.names:
            if alias.name == '*':
                if hit:
                    unknown.append((node.lineno, 'STAR_IMPORT'))
                bindings.append(('*', None))
                continue
            full = (module + '.' if module else '') + alias.name
            if hit and alias.name == contract:
                uses.append((node.lineno, 'IMPORT', not guarded))
            member = _names_producer(modules, path, full, relative, producer) if hit is False else False
            if member is None:
                unknown.append((node.lineno, 'ENVIRONMENT_DEPENDENT_IMPORT'))
            name = alias.asname or alias.name
            bindings.append((name, _Binding('IMPORT', name, full, member)))
        return bindings

    sites = _scan_scopes(tree, imported)

    def reference(scope, expr):
        """'PRODUCER' when ``expr`` provably names the producer module, 'AMBIGUOUS' when it may, else None."""
        dotted = _dotted(expr)
        if dotted is None:
            return None
        bindings = scope.lookup(dotted.split('.')[0])
        # An environment-dependent import is already UNKNOWN at its own line.
        if not any(b.kind == 'IMPORT' and b.bound == dotted and b.hit for b in bindings):
            return None
        if all(b.kind == 'IMPORT' and b.identity == bindings[0].identity for b in bindings):
            return 'PRODUCER'
        return 'AMBIGUOUS'

    def qualified(scope, expr):
        """The provable qualified name of a callable, or None."""
        dotted = _dotted(expr)
        if dotted is None:
            return None
        head = dotted.split('.')[0]
        bindings = scope.lookup(head)
        if not bindings:
            return dotted if hasattr(builtins, head) else None
        if all(b.kind == 'IMPORT' and b.identity == bindings[0].identity for b in bindings):
            return bindings[0].identity + dotted[len(head):]
        return None

    def creates(node, position):
        # ``create`` lets patch add a missing attribute: unless it is literally false, nothing breaks.
        return len(node.args) > position or any(
            k.arg is None or k.arg == 'create' and not (isinstance(k.value, ast.Constant) and k.value.value is False)
            for k in node.keywords)

    for node, scope, guarded in sites:
        line = node.lineno
        if isinstance(node, ast.Attribute):
            if node.attr != contract:
                continue
            state = reference(scope, node.value)
            writes = isinstance(node.ctx, ast.Store) and id(node) not in augmented
            if state and writes:
                recreations.append(line)
            if state == 'PRODUCER':
                uses.append((line, 'ATTRIBUTE', not guarded and not writes))
            elif state:
                unknown.append((line, 'AMBIGUOUS_BINDING'))
            continue
        if not node.args:
            continue
        name, parts = qualified(scope, node.func), (_dotted(node.func) or '').split('.')
        first = _literal(node.args[0])
        second = _literal(node.args[1]) if len(node.args) > 1 else None
        if parts[-1] == 'setattr':
            # Any ``setattr`` (the builtin, ``monkeypatch``'s...) of the name, or of a dynamic one, may recreate it.
            if first in targets or (len(node.args) > 1 and reference(scope, node.args[0])
                                    and second in (None, contract)):
                recreations.append(line)
        elif name == 'getattr' or parts == ['getattr']:
            state = reference(scope, node.args[0])
            if len(node.args) < 2 or not state or second not in (None, contract):
                continue
            if state != 'PRODUCER':
                unknown.append((line, 'AMBIGUOUS_BINDING'))
            elif name != 'getattr':
                unknown.append((line, 'UNRESOLVED_CALL'))
            elif second is None:
                unknown.append((line, 'DYNAMIC_ATTRIBUTE'))
            else:
                # A default handles the missing attribute.
                uses.append((line, 'GETATTR', not guarded and len(node.args) == 2 and not node.keywords))
        elif name in patches or parts[-1] == 'patch':
            if first not in targets:
                continue
            if creates(node, 3):
                recreations.append(line)
            if name in patches:
                uses.append((line, 'PATCH_TARGET', not guarded and not creates(node, 3)))
            else:
                unknown.append((line, 'UNRESOLVED_PATCH'))
        elif name in objects or parts[-2:] == ['patch', 'object']:
            state = reference(scope, node.args[0])
            if not state or second != contract:
                continue
            if creates(node, 4):
                recreations.append(line)
            if state == 'PRODUCER' and name in objects:
                uses.append((line, 'PATCH_TARGET', not guarded and not creates(node, 4)))
            else:
                unknown.append((line, 'UNRESOLVED_PATCH' if state == 'PRODUCER' else 'AMBIGUOUS_BINDING'))
    return sorted(set(uses)), sorted(set(unknown)), sorted(set(recreations))


def _related_tasks(tasks, task_id):
    """The task itself, its ancestors and its descendants: tasks ordered with it."""
    def closure(start, edges):
        found, pending = set(), list(edges(start))
        while pending:
            current = pending.pop()
            if current not in found and current in tasks:
                found.add(current)
                pending.extend(edges(current))
        return found
    ancestors = closure(task_id, lambda t: tasks[t].get('depends_on', []))
    descendants = closure(task_id, lambda t: [k for k, v in tasks.items() if t in v.get('depends_on', [])])
    return {task_id} | ancestors | descendants


def _obligations(task):
    return dict(requirement_refs=sorted(set(task.get('ac_refs', [])) | set(task.get('sec_refs', []))),
                standard_refs=sorted(task.get('standard_refs', [])),
                quality_check_refs=sorted((task.get('verification') or {}).get('quality_check_refs', [])))


def _contract_analysis(plan, files, texts, modules):
    """Verify each declared contract change against the static uses at the plan base.

    A consumer whose incompatibility is demonstrated (an unguarded use of a
    ``REMOVED`` name) and that no task covers, or that only a task unordered
    with the declaring one covers, is a defect bound to the declaring task's
    approved obligations. Without such obligations, for uses that may survive
    the change (any ``INCOMPATIBLE`` use, guarded or fallback uses, uses in a
    file that may recreate the name) and for
    every undecidable case, the result is a non-blocking diagnosis.
    """
    tasks = {t['task_id']: t for t in plan['tasks']}
    trees = {}
    for path in sorted(texts):
        if path.endswith('.py'):
            try:
                trees[path] = ast.parse(texts[path])
            except (SyntaxError, ValueError):
                pass
    declarations, defects, diagnoses = [], [], []
    for task in plan['tasks']:
        related = _related_tasks(tasks, task['task_id'])
        for item in task.get('contract_changes', []):
            contract, producer, change = item['contract'], item['producer'], item['expected_change']
            subject = dict(task_id=task['task_id'], contract=contract, producer=producer, expected_change=change)
            reason = None
            if not _IDENTIFIER.fullmatch(contract):
                reason = 'ATTRIBUTE_CONTRACT' if all(_IDENTIFIER.fullmatch(p) for p in contract.split('.')) \
                    else 'GENERIC_CONTRACT'
            elif producer not in files:
                reason = 'PRODUCER_NOT_AT_BASE'
            elif not producer.endswith('.py'):
                reason = 'NON_PYTHON_PRODUCER'
            elif producer not in trees:
                reason = 'UNREADABLE_PRODUCER'
            elif change in BREAKING_CHANGES and contract not in _top_level_names(trees[producer]):
                reason = 'CONTRACT_NOT_AT_BASE'
            if reason:
                declarations.append(dict(subject, status='UNKNOWN', reason=reason, consumers=[]))
                diagnoses.append(dict(subject, reason=reason, path=producer, line=None))
                continue
            consumers = []
            for path, tree in trees.items():
                if path == producer:
                    continue
                uses, unknown, recreations = _contract_uses(path, tree, modules, producer, contract)
                for line, why in unknown:
                    diagnoses.append(dict(subject, reason=why, path=path, line=line))
                if not uses:
                    continue
                # A file that may give the producer the name back no longer shows
                # which of its reads need the original one.
                breaking = [(line, via) for line, via, breaks in uses if breaks and not recreations]
                # Only a removal makes a use fail statically: the new shape of an
                # INCOMPATIBLE contract is not known at the base, and a guarded,
                # fallback or recreated use survives the removal.
                incompatibility = ('NOT_EXPECTED' if change not in BREAKING_CHANGES
                                   else 'DEMONSTRATED' if change == 'REMOVED' and breaking else 'NOT_DEMONSTRATED')
                line, via = breaking[0] if incompatibility == 'DEMONSTRATED' else uses[0][:2]
                covering = sorted(k for k, t in tasks.items() if path in t['target_files'])
                coverage = ('UNCOVERED' if not covering else 'COVERED' if set(covering) & related
                            else 'UNORDERED')
                consumers.append(dict(path=path, line=line, via=via, coverage=coverage, covering_task_ids=covering,
                                      incompatibility=incompatibility))
                if change not in BREAKING_CHANGES or coverage == 'COVERED':
                    continue
                if incompatibility != 'DEMONSTRATED':
                    diagnoses.append(dict(subject, reason='INCOMPATIBILITY_NOT_DEMONSTRATED', path=path, line=line))
                    continue
                gap = 'OWNER_OR_SCOPE' if coverage == 'UNCOVERED' else 'ORDERING'
                obligations = _obligations(task)
                if any(obligations.values()):
                    defects.append(dict(subject, category='PLAN_DEFECT', consumer=path, line=line, via=via,
                                        gap=gap, covering_task_ids=covering, **obligations))
                else:
                    diagnoses.append(dict(subject, reason='NO_APPROVED_OBLIGATION', path=path, line=line))
            declarations.append(dict(subject, status='ANALYZED', consumers=consumers))
    order = lambda row: (row['task_id'], row['producer'], row['contract'])
    return dict(declarations=sorted(declarations, key=order),
                defects=sorted(defects, key=lambda d: (*order(d), d['consumer'])),
                diagnoses=sorted(diagnoses, key=lambda d: (*order(d), d['path'], d['line'] or 0, d['reason'])))


def contract_defects(report):
    """Demonstrated contract incompatibilities that the plan review must report as blockers."""
    return list((report.get('contracts') or {}).get('defects', []))


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


PLAN_REF = '02-design/plan.json'
CONTINUATION_REPORT_REF = '02-design/impact-report.json'


def report_refs(plan_sha):
    """Where normal planning (named by plan digest) and continuations register the report."""
    return ('02-design/task-planning/impact-report-' + plan_sha + '.json', CONTINUATION_REPORT_REF)


def registered_report(root, registered, plans):
    """The first registered, intact report bound to one of ``plans`` (closest first).

    ``registered`` maps refs to the SHA-256 recorded in the root's ``state.json``.
    A report counts only when its ``plan_sha256`` names that plan. A registered
    report whose bytes changed is an error, not a missing report.
    """
    from ssdlc_transitions import _regular
    for plan_sha in plans:
        for ref in report_refs(plan_sha):
            if ref not in registered:
                continue
            path = _regular(Path(root), ref)
            if file_hash(path) != registered[ref]:
                raise TaskError('Registered impact report changed')
            report = json.loads(path.read_text())
            if isinstance(report, dict) and report.get('kind') == KIND and report.get('plan_sha256') == plan_sha:
                return report
    return None


def plan_parents(chain):
    """Extended plan digest -> the plan it was extended from, per execution generation."""
    parents = {}
    for item in chain:
        subject = (item['recipe'] or {}).get('subject')
        if isinstance(subject, dict):
            new, old = subject.get('new_plan_sha256'), subject.get('old_plan_sha256')
            if isinstance(new, str) and isinstance(old, str) and new != old:
                parents.setdefault(new, old)
    return parents


def plan_lineage(plan_sha, parents):
    """``plan_sha`` and its ancestors, closest first; the last is the originating plan."""
    lineage = [plan_sha]
    while parents.get(lineage[-1]) is not None and parents[lineage[-1]] not in lineage:
        lineage.append(parents[lineage[-1]])
    return lineage


def source_lineage(source):
    """Plan lineage of a feedback source; unreadable lineage keeps the plan alone."""
    plan_sha = digest(source['plan'])
    root = Path(source['root'])
    if root.parent.name != 'execution-runtimes':
        return [plan_sha]
    try:
        return plan_lineage(plan_sha, plan_parents(_transitions(root.parent.parent)))
    except (TaskError, OSError, ValueError, KeyError, TypeError):
        return [plan_sha]


def _continuation_root(ticket):
    """The selected continuation runtime, validated like ``runtime_root``."""
    store = ticket / 'continuations'
    if not (store / 'active.json').exists():
        return None
    from ssdlc_transitions import TransitionStore
    projection = TransitionStore(store).load()
    identity = projection.get('transition_id', '')
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise TaskError('Active continuation identity is missing')
    root = ticket / 'runtime-generations' / identity
    if _read_json(root / 'generation.json') != projection['runtime_binding']:
        raise TaskError('Runtime generation binding changed')
    return root


def _registrations(ticket, chain):
    """(root, registered refs) of the ticket, its continuation and its generations, oldest first."""
    roots = [ticket]
    continuation = _continuation_root(ticket)
    if continuation is not None:
        roots.append(continuation)
    roots.extend(ticket / 'execution-runtimes' / item['transition_id'] for item in chain)
    result = []
    for root in roots:
        if root == ticket and not (root / 'state.json').exists():
            continue  # A continuation target ticket keeps its state in the runtime.
        state = _read_json(root / 'state.json')
        result.append((root, {ref: meta['sha256'] for ref, meta in state.get('artifacts', {}).items()}))
    return result


def _registered_plan(root, registered):
    from ssdlc_transitions import _regular
    if PLAN_REF not in registered:
        return None
    path = _regular(root, PLAN_REF)
    if file_hash(path) != registered[PLAN_REF]:
        raise TaskError('Registered plan changed')
    return digest(json.loads(path.read_text()))


def _avoidable(ticket, chain, extensions):
    """Avoidable omissions per originating plan of the ticket's runtime genealogy.

    A plan extended by an execution generation belongs to the plan it was
    extended from; planning and continuations originate plans with their own
    report. Feedback is attributed through its bound plan, extensions through
    the plan they extended.
    """
    registrations = _registrations(ticket, chain)
    parents = plan_parents(chain)
    plans = [sha for root, registered in registrations
             for sha in [_registered_plan(root, registered)] if sha is not None]
    if not plans:
        return [dict(plan_sha256=None, report_sha256=None, status='UNKNOWN', count=None, paths=[],
                     reason='NO_REGISTERED_PLAN')]
    groups = {plan_lineage(sha, parents)[-1]: {} for sha in plans}
    def observe(plan_sha, path, source):
        origin = plan_lineage(plan_sha, parents)[-1] if isinstance(plan_sha, str) else None
        groups.setdefault(origin, {}).setdefault(path, set()).add(source)
    if (ticket / 'feedback/authority.json').exists():
        from ssdlc_feedback import FeedbackStore
        for row in FeedbackStore(ticket).inspect()['requests'].values():
            if row['request']['cause'] == 'MISSING_TARGET':
                for path in row['request']['requested_files']:
                    observe(row['binding']['plan_sha256'], path, 'FEEDBACK_MISSING_TARGET')
    subjects = {item['transition_id']: (item['recipe'] or {}).get('subject') or {} for item in chain}
    for row in extensions:
        for path in row['added_paths']:
            observe(subjects.get(row['transition_id'], {}).get('old_plan_sha256'), path, 'MANUAL_EXTENSION')
    result = []
    for origin in sorted(groups, key=lambda sha: (sha is None, sha or '')):
        report = None
        if origin is not None:
            report = next((found for root, registered in registrations
                           for found in [registered_report(root, registered, [origin])] if found is not None), None)
        if report is None:
            result.append(dict(plan_sha256=origin, report_sha256=None, status='UNKNOWN', count=None, paths=[],
                               reason='NO_IMPACT_REPORT' if origin else 'NO_ORIGINATING_PLAN'))
            continue
        observed = groups[origin]
        flagged = {row['path'] for row in report['possible_omissions']}
        paths = [dict(path=p, observed_in=sorted(observed[p])) for p in sorted(flagged & set(observed))]
        result.append(dict(plan_sha256=origin, report_sha256=digest(report), status='MEASURED',
                           count=len(paths), paths=paths))
    return result


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
    unknown = lambda reason: [dict(plan_sha256=None, report_sha256=None, status='UNKNOWN', count=None,
                                   paths=[], reason=reason)]
    if extensions is None:
        avoidable = unknown('EXECUTION_LINEAGE_UNAVAILABLE')
        delta = dict(status='UNKNOWN', reason='EXECUTION_LINEAGE_UNAVAILABLE')
    else:
        try:
            avoidable = _avoidable(ticket, chain, extensions)
        except errors:
            avoidable = unknown('EVIDENCE_UNAVAILABLE')
        try:
            delta = _profiles(ticket, chain)
        except errors:
            delta = dict(status='UNKNOWN', reason='PROFILE_UNAVAILABLE')
    return dict(avoidable_omissions=avoidable, profile_delta=delta, manual_extension_pending_tasks=pending)
