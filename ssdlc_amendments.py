"""Deterministic plan-amendment checks; review remains a separate authority.

The advisory impact report that replaced ``fixture_impact`` lives in
``ssdlc_impact``.
"""
from ssdlc_planning import validate_plan_review
from ssdlc_tasks import PlanError, digest


def ancestors(plan):
    tasks = {t['task_id']: t for t in plan['tasks']}
    result, visiting = {}, set()
    def visit(tid):
        if tid in visiting or tid not in tasks:
            raise PlanError('Amendment contains a cycle or unknown dependency')
        if tid in result:
            return result[tid]
        visiting.add(tid)
        inherited = set()
        for parent in tasks[tid]['depends_on']:
            inherited.add(parent)
            inherited.update(visit(parent))
        visiting.remove(tid)
        result[tid] = inherited
        return inherited
    for tid in tasks:
        visit(tid)
    return result



def scope_diff(old, new, lineage=None):
    """Host-owned literal path changes; a reviewer digest is not a scope diff."""
    before = {t['task_id']: set(t.get('target_files', [])) for t in old['tasks']}
    after = {t['task_id']: set(t.get('target_files', [])) for t in new['tasks']}
    mapping = lineage or {tid: [tid] for tid in before}
    inherited = {tid: set() for tid in after}
    for tid, targets in mapping.items():
        for target in targets:
            if target in inherited:
                inherited[target].update(before.get(tid, set()))
    return {tid: {'added': sorted(paths - inherited[tid]), 'removed': sorted(inherited[tid] - paths)}
            for tid, paths in sorted(after.items()) if paths != inherited[tid]}

def validate_amendment(old, new, report, validator, requirements, quality, *, lineage=None, history=(), declared_scope=None):
    """Require reviewed obligations and preserve ordering through merges/splits.

    Lineage maps each old node to its replacement nodes. This validates DAG
    semantics only; execution allowances still need an explicit mapping, so an
    executor may refuse lineage changes it cannot account for safely.
    """
    if new['plan_revision'] != old['plan_revision'] + 1:
        raise PlanError('Amendment must advance the plan revision exactly once')
    ordered = validator(new)
    before, after = ancestors(old), ancestors(new)
    if lineage is None:
        if set(before) != set(after):
            raise PlanError('Task merges/splits require explicit lineage')
        lineage = {tid: [tid] for tid in before}
    if (not isinstance(lineage, dict) or set(lineage) != set(before)
            or any(not isinstance(nodes, list) or not nodes
                   or any(not isinstance(n, str) for n in nodes) or len(set(nodes)) != len(nodes)
                   for nodes in lineage.values())
            or set().union(*(set(nodes) for nodes in lineage.values())) != set(after)):
        raise PlanError('Incomplete task lineage')
    for child, predecessors in before.items():
        for parent in predecessors:
            for replacement in lineage[child]:
                for prerequisite in lineage[parent]:
                    if prerequisite != replacement and prerequisite not in after[replacement]:
                        raise PlanError('Amendment lost required transitive ordering')
    changes = scope_diff(old, new, lineage)
    if any(change['added'] for change in changes.values()) and declared_scope != changes:
        raise PlanError('Scope expansion must be explicitly declared with the exact host scope diff')
    if declared_scope is not None and declared_scope != changes:
        raise PlanError('Declared scope differs from the host scope diff')
    assessment = validate_plan_review(report, new, requirements, list(history), quality)
    if assessment['verdict'] != 'APPROVED':
        raise PlanError('Amendment has unresolved independent review findings')
    return {'old_plan_sha256': digest(old), 'new_plan_sha256': digest(new),
            'assessment': assessment, 'lineage': lineage, 'scope_diff': changes,
            'ordered_task_ids': [t['task_id'] for t in ordered]}
