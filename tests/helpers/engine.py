"""The SSDLC engine that left ``main`` with R1 of ADR 0007 (#538).

The engine stays in the Git history and in the tag ``ssdlc-engine-final``, the last commit of
``main`` that holds it. Its paths are listed here once, for the test that none is left on
``main`` and that devloop, ``ops/`` and ``scripts/`` name none (``tests/test_engine_removed.py``),
and for the documentation check, which accepts them in the ADRs only, as history
(``tests/test_doc_links.py``).
"""

import fnmatch
import re

TAG = 'ssdlc-engine-final'

# Files at the root of the repository, as patterns.
ROOT_FILES = ('orchestrator.py', 'ssdlc_*.py', 'setup-ssdlc.sh', 'PLAN_*.md', 'Dockerfile',
              'requirements.txt')
# Directories: every path under them.
DIRECTORIES = ('schemas', 'standards', 'docs/evidence', 'tests/replay', 'tests/fixtures/quota',
               'tests/fixtures/recovery-history')
# The engine's documents.
DOCUMENTS = tuple(f'docs/{name}.md' for name in (
    'bounce-preview', 'bounded-recovery', 'candidate-capture', 'configuration', 'continuations',
    'cost-telemetry', 'developer-feedback', 'getting-started', 'historical-reconciliation',
    'limit-extensions', 'native-gate-convergence', 'recovery', 'scope-amendments', 'supervision',
    'task-mode', 'troubleshooting', 'workflow-status', 'writer-fencing'))
# The engine's tests, their helpers and fixtures.
TESTS = tuple(f'tests/{name}' for name in (
    'docker_pid_smoke.py', 'docker_smoke.py', 'helpers/environment_cache.py',
    'helpers/host_commands.py', 'helpers/supervisor_fakes.py',
    'fixtures/rejected_pair_design_r5.json', 'fixtures/rejected_plan_review_r5.json',
    'fixtures/rejected_step3a2_references.json',
    'test_agy_auth.py', 'test_amendments.py', 'test_automatic_fencing.py',
    'test_baseline_refusal_evidence.py', 'test_bootstrap.py', 'test_bootstrap_preparation.py',
    'test_bounce_evidence.py', 'test_bounce_planner.py', 'test_bounce_preview.py',
    'test_candidate_evidence.py', 'test_claude_auth.py', 'test_container_holder.py',
    'test_container_holder_docker.py', 'test_container_resources.py',
    'test_continuation_hardening.py', 'test_continuations.py', 'test_contract_changes.py',
    'test_core_equivalence.py', 'test_corrective_bounded_accounting.py',
    'test_corrective_capture_identity.py', 'test_corrective_creation.py',
    'test_corrective_executor.py', 'test_corrective_lineage.py',
    'test_corrective_owner_integration.py', 'test_corrective_publication.py',
    'test_corrective_recovery.py', 'test_cost_telemetry.py', 'test_credit_caps.py',
    'test_design_recovery.py', 'test_design_session.py', 'test_dev_repair.py', 'test_feedback.py',
    'test_feedback_admission.py', 'test_feedback_analysis.py', 'test_feedback_bindings.py',
    'test_feedback_boundaries.py', 'test_feedback_delegation.py', 'test_feedback_delivery.py',
    'test_feedback_host_hint.py', 'test_feedback_native.py', 'test_feedback_return.py',
    'test_feedback_roles.py', 'test_feedback_workflow.py', 'test_full_checkpoint_review.py',
    'test_gate_convergence_integrity.py', 'test_gate_diagnosis.py',
    'test_gate_diagnosis_history.py', 'test_gate_findings.py', 'test_gate_report_protocol.py',
    'test_gate_report_regressions.py', 'test_gate_reports.py', 'test_gate_sequence.py',
    'test_historical_accounting.py', 'test_host_command_fencing.py',
    'test_i2_corrective_integration.py', 'test_i2_hardening.py', 'test_impact.py',
    'test_issue32_integrity.py', 'test_journal_integrity.py', 'test_legacy_compat.py',
    'test_limit_extensions.py', 'test_linked_toolkit.py', 'test_live_usage_limits.py',
    'test_lock_leases.py', 'test_manual_host_scope.py', 'test_not_dispatched.py',
    'test_operator_profile_recovery.py', 'test_opsdocs_context.py',
    'test_owner_operator_recovery.py', 'test_owner_revalidation.py', 'test_phase_boundary.py',
    'test_plan_references.py', 'test_plan_repair.py', 'test_planning_convergence.py',
    'test_po_validation.py', 'test_positive_test_proof.py', 'test_pr71_review_fixes.py',
    'test_project_env_template.py', 'test_quota_snapshots.py', 'test_reconciliation.py',
    'test_recovery_benchmark.py', 'test_recovery_docs.py', 'test_recovery_limits.py',
    'test_repair_prompt.py', 'test_retry_logging.py', 'test_review_closure.py',
    'test_review_verdict.py', 'test_role_fencing.py', 'test_scheduler_extractions.py',
    'test_scope_amendments.py', 'test_separate_caps.py', 'test_signoff_provenance.py',
    'test_stage_observability.py', 'test_supervisor_cancel_resume.py', 'test_supervisor_core.py',
    'test_supervisor_dispatch.py', 'test_supervisor_preflight.py', 'test_task_bootstrap.py',
    'test_task_budget.py', 'test_task_repair.py', 'test_timeout_reverification.py',
    'test_transitional_expectations.py', 'test_transitions.py', 'test_unified_execution.py',
    'test_unified_integration.py', 'test_verification_repair.py', 'test_verification_timeout.py',
    'test_workflow_cli.py', 'test_workflow_liveness.py', 'test_workflow_projection.py',
    'test_workflow_status.py', 'test_writer_gate.py'))
# A root module of the engine imported by Python code.
ENGINE_IMPORT = re.compile(r'^\s*(?:import|from)\s+(orchestrator|ssdlc_\w+)\b', re.M)
# The candidate paths in a text: runs of the characters of a path.
_TOKEN = re.compile(r'[\w./*-]+')


def normalized(path):
    """``path`` relative to the root, without ``./``, a trailing ``/`` or an anchor."""
    path = path.strip().partition('#')[0]
    while path.startswith('./'):
        path = path[2:]
    return path.rstrip('/')


def is_engine_path(path):
    """True when ``path`` (relative to the root) is a path of the engine, or a pattern that names
    only paths of the engine."""
    path = normalized(path)
    if not path:
        return False
    if '/' not in path and any(fnmatch.fnmatchcase(path, p) for p in ROOT_FILES):
        return True
    if path in DOCUMENTS or path in TESTS:
        return True
    return any(path == d or path.startswith(d + '/') for d in DIRECTORIES)


def references(text):
    """The paths of the engine that ``text`` names, in order. A bare ``schemas/`` or
    ``standards/`` is not one: they are common names of directories, which devloop keeps out of
    level 0 for any project (``devloop/policy.py``); a path under them is."""
    found = []
    for match in _TOKEN.finditer(text):
        token = match.group().strip('.')
        if normalized(token) in ('schemas', 'standards'):
            continue
        if is_engine_path(token):
            found.append(token)
    return found
