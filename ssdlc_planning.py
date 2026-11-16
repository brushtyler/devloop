"""Bounded, restartable plan repair. Callers own the ticket lock and gate review."""
import json
from pathlib import Path

from ssdlc_tasks import PlanError, atomic_json, confined, digest, file_hash, parse_plan_output, log_retry_reason, validate_document

# A pass without progress is a review whose non-empty set of open blocking IDs
# equals the set of the previous review. Two such passes (three identical sets
# in a row) stop the session as STAGNATION.
NO_PROGRESS_LIMIT = 2


def no_progress_passes(signatures: list[list[str]], current: list[str]) -> int:
    """Count the consecutive earlier reviews that repeat the current open blocker set."""
    passes = 0
    if current:
        for previous in reversed(signatures):
            if previous != current:
                break
            passes += 1
    return passes


def describe_no_progress(counter) -> str:
    if not counter:
        return 'no-progress passes: not recorded'
    return (f"no-progress passes {counter['passes']}/{counter['limit']} at candidate {counter['attempt']}"
            f" (open blockers: {', '.join(counter['blocker_ids']) or 'none'})")


def unreported_contract_defects(findings: list[dict], defects) -> list[dict]:
    """Host-demonstrated contract defects that no OPEN BLOCKING PLAN_DEFECT reports.

    A finding reports a defect when it names the declaring task, cites the
    consumer path in its message or evidence and shares at least one approved
    obligation reference with the defect.
    """
    missing = []
    for defect in defects:
        if not any(f['status'] == 'OPEN' and f['severity'] == 'BLOCKING' and f['category'] == 'PLAN_DEFECT'
                   and defect['task_id'] in f['task_ids']
                   and defect['consumer'] in f['message'] + '\n' + f['evidence']
                   and any(set(f.get(field, [])) & set(defect[field])
                           for field in ('requirement_refs', 'standard_refs', 'quality_check_refs'))
                   for f in findings):
            missing.append(defect)
    return missing


def validate_plan_review(report: str, plan: dict, requirements: list[str], history: list[dict], quality=None,
                         schema_name='task-plan-review', contract_defects=()) -> dict:
    """Require attributable findings and explicit dispositions of earlier blockers.

    ``contract_defects`` are the impact report's demonstrated contract
    incompatibilities: each needs an OPEN BLOCKING ``PLAN_DEFECT``, so the plan
    cannot be approved while one remains.
    """
    result = parse_plan_output(report)
    validate_document(schema_name, result)
    findings = result['findings']
    ids = [f['finding_id'] for f in findings]
    if len(ids) != len(set(ids)):
        raise PlanError('Duplicate review finding_id')
    task_ids = {t['task_id'] for t in plan['tasks']}
    quality = quality or {}
    standards = {s['standard_id'] for s in quality.get('standards', [])}
    checks = {c['check_id'] for c in quality.get('checks', [])}
    assessed = [h['assessment'] for h in history if 'assessment' in h]
    previous = assessed[-1]['findings'] if assessed else []
    old_ids = {f['finding_id'] for h in history for f in h.get('assessment', {}).get('findings', [])}
    old_findings = {f['finding_id']: f for assessment in assessed for f in assessment['findings']}
    outstanding = {f['finding_id'] for f in previous if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING'}
    if not outstanding <= set(ids):
        raise PlanError('Review omitted prior blocking findings: ' + ', '.join(sorted(outstanding - set(ids))))
    for finding in findings:
        old = old_findings.get(finding['finding_id'])
        if old and (any(set(old.get(field, [])) != set(finding.get(field, []))
                        for field in ('requirement_refs', 'standard_refs', 'quality_check_refs'))
                    or old['category'] != finding['category'] or old['severity'] != finding['severity']):
            raise PlanError('Review reassigned an existing finding ID; resolve it explicitly and use a new ID')
        if not set(finding['requirement_refs']) <= set(requirements) or not set(finding['task_ids']) <= task_ids:
            raise PlanError('Review references unknown requirements or tasks')
        if not set(finding.get('standard_refs', [])) <= standards or not set(finding.get('quality_check_refs', [])) <= checks:
            raise PlanError('Review references unknown standards or quality checks')
        if finding['severity'] == 'BLOCKING' and finding['status'] == 'OPEN':
            if (not any(finding.get(field) for field in ('requirement_refs', 'standard_refs', 'quality_check_refs'))
                    or finding['category'] == 'IMPROVEMENT'):
                raise PlanError('Blocking finding needs an approved requirement or quality obligation and cannot be an optional improvement')
        if assessed and finding['finding_id'] not in old_ids and finding['new_issue_reason'] == 'INITIAL_REVIEW':
            raise PlanError('New review finding must explain introduction by revision or earlier omission')
    unreported = unreported_contract_defects(findings, contract_defects)
    if unreported:
        raise PlanError('Review omitted demonstrated contract incompatibilities: report each as an OPEN BLOCKING '
                        'PLAN_DEFECT naming the declaring task, the consumer path and an approved obligation of the defect',
                        [{'code': 'CONTRACT_DEFECT_NOT_REPORTED', **defect} for defect in unreported])
    blockers =[f for f in findings if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
    if (result['verdict'] == 'APPROVED') != (not blockers):
        raise PlanError('Review verdict contradicts its open blocking findings')
    return result


class PlanRepair:
    def __init__(self, directory: Path, inputs: dict, validator, generate, review, budget, review_repairs=None,
                 structured_reviews=False, contract_defects=None):
        self.root = directory / 'task-planning'
        self.root.mkdir(exist_ok=True)
        self.path = self.root / 'session.json'
        self.inputs = inputs
        self.validator = validator
        self.generate = generate
        self.review = review
        self.budget = budget
        if review_repairs is not None and (type(review_repairs) is not int or not 0 <= review_repairs <= 4):
            raise PlanError('review_repairs must be an integer between 0 and 4')
        self.review_repairs = review_repairs
        self.structured_reviews = structured_reviews
        # plan -> demonstrated contract defects the review must report (from the impact report).
        self.contract_defects = contract_defects or (lambda plan: [])

    def _fingerprint(self):
        return digest({'inputs': self.inputs, 'review_protocol': 'structured-v1'}) if self.structured_reviews else digest(self.inputs)

    def _verify_evidence(self, session):
        for record in session['attempts']:
            for hash_field in [key for key in record if key.endswith('_sha256')]:
                field = hash_field[:-len('_sha256')]
                path = confined(self.root, record[field], protect=False)
                if not path.is_file() or file_hash(path) != record[hash_field]:
                    raise PlanError('Planning evidence changed or disappeared')
        for recovery in session.get('recoveries', []):
            for field, hash_field in [('prior_session_ref', 'prior_session_sha256'), ('inputs_ref', 'inputs_file_sha256')]:
                path = confined(self.root, recovery[field], protect=False)
                if not path.is_file() or file_hash(path) != recovery[hash_field]:
                    raise PlanError('Planning recovery evidence changed or disappeared')

    def recover_rejected_review(self):
        """Explicitly revalidate a captured rejection after a validator correction.

        This preserves reservations and raw evidence, records the input revision,
        and only resumes repairs of an independently rejected plan. It cannot
        turn a protocol failure into an approval.
        """
        if not self.structured_reviews or not self.path.is_file():
            raise PlanError('Review recovery requires an existing structured planning session')
        session = json.loads(self.path.read_text())
        self._verify_evidence(session)
        if not session['attempts']:
            raise PlanError('No rejected review to recover')
        record = session['attempts'][-1]
        if record.get('failure_kind') != 'REVIEW_PROTOCOL' or record['status'] != 'REJECTED':
            raise PlanError('Only a persisted review protocol failure can be recovered')
        plan = parse_plan_output((self.root / record['candidate']).read_text())
        self.validator(plan)
        context = self.inputs['context']
        report = (self.root / record['review']).read_text()
        history = self._history(session)[:-1]
        assessment = validate_plan_review(report, plan, context['requirement_catalogue']['required_ids'], history,
                                          context.get('quality_contract'),
                                          contract_defects=self.contract_defects(plan))
        if assessment['verdict'] != 'REVISION_REQUIRED':
            raise PlanError('Review recovery cannot grant approval; a rejected review with open blockers is required')
        index = len(session.get('recoveries', [])) + 1
        prior = self.root / f'recovery-{index}-prior-session.json'
        inputs = self.root / f'recovery-{index}-inputs.json'
        if prior.exists() or inputs.exists():
            raise PlanError('Incomplete review recovery requires reconciliation; refusing overwrite')
        atomic_json(prior, session)
        atomic_json(inputs, self.inputs)
        session.setdefault('recoveries', []).append({
            'reason': 'Explicit revalidation of a captured rejected review after validator correction',
            'previous_inputs_sha256': session['inputs_sha256'], 'inputs_sha256': self._fingerprint(),
            'prior_session_ref': prior.name, 'prior_session_sha256': file_hash(prior),
            'inputs_ref': inputs.name, 'inputs_file_sha256': file_hash(inputs),
            'attempt': record['attempt'], 'outcome': 'REVISION_REQUIRED; no approval granted',
        })
        session['inputs_sha256'] = self._fingerprint()
        record['assessment'] = assessment
        record['previous_failure_kind'] = record.pop('failure_kind')
        if 'diagnostics' in record:
            record['historical_diagnostics'] = record.pop('diagnostics')
            record['historical_diagnostics_sha256'] = record.pop('diagnostics_sha256')
        atomic_json(self.path, session)
        print(f"[task-plan] Recovered rejected review for candidate {record['attempt']}; reservations retained, open findings sent to planner.", flush=True)

    def _history(self, session):
        return [{**{k: record[k] for k in ('attempt', 'status', 'candidate_sha256') if k in record},
                 **{field: (self.root / record[field]).read_text() for field in ('diagnostics', 'review') if field in record},
                 **({'assessment': record['assessment']} if 'assessment' in record else {})}
                for record in session['attempts'] if 'review' in record or 'diagnostics' in record]

    def _structured_review(self, session, record, plan, history):
        feedback = history
        for attempt in range(1, 3):
            record['review_invocations'] = attempt
            atomic_json(self.path, session)
            self.budget()
            report = self.review(plan, feedback)
            self._save_evidence(session, record, f'review-response-{attempt}', report)
            self.budget()
            try:
                assessment = validate_plan_review(report, plan, self.inputs['context']['requirement_catalogue']['required_ids'], history,
                                                  self.inputs['context'].get('quality_contract'),
                                                  contract_defects=self.contract_defects(plan))
                return report, assessment
            except PlanError as exc:
                errors = json.dumps(exc.diagnostics, indent=2)
                self._save_evidence(session, record, f'review-error-{attempt}', errors)
                if attempt == 2:
                    self._save_evidence(session, record, 'review', report)
                    self._save_evidence(session, record, 'diagnostics', errors)
                    record.update(status='REJECTED', failure_kind='REVIEW_PROTOCOL')
                    atomic_json(self.path, session)
                    raise PlanError('Invalid review protocol after one correction: ' + str(exc)) from exc
                print(f'[task-plan] Review protocol invalid: {exc}; correcting review once on the same candidate.', flush=True)
                feedback = history + [{'attempt': record['attempt'], 'invalid_response': report,
                                       'review_protocol_errors': exc.diagnostics,
                                       'instruction': 'Correct only the review response against the schema and history; the plan is unchanged.'}]

    def run(self, initial_candidate=None):
        fingerprint = self._fingerprint()
        existing = self.path.exists()
        session = json.loads(self.path.read_text()) if existing else {
            'schema_version': '1.0', 'inputs_sha256': fingerprint, 'attempts': [],
        }
        if session['inputs_sha256'] != fingerprint:
            raise PlanError('Planning inputs changed; a new approved planning revision is required')
        if initial_candidate is not None and not session['attempts']:
            record = {'attempt': 1, 'status': 'CANDIDATE', 'origin': 'previous_archived_candidate'}
            session['attempts'].append(record)
            self._save_evidence(session, record, 'candidate', initial_candidate)
        self._verify_evidence(session)
        # Old sessions retain their original total bound unless explicitly extended.
        default_repairs = 0 if existing else 4 if self.structured_reviews else 2
        if not existing and self.review_repairs is not None:
            default_repairs = self.review_repairs
        limits = session.setdefault('limits', {'structural_candidates': 3, 'review_repairs': default_repairs})
        if self.review_repairs is not None and self.review_repairs != limits['review_repairs']:
            if self.review_repairs < limits['review_repairs']:
                raise PlanError('Cannot reduce persisted planning limits during resume')
            session.setdefault('policy_updates', []).append({
                'previous_review_repairs': limits['review_repairs'], 'review_repairs': self.review_repairs,
                'reason': 'Explicit planning resume configuration',
            })
            limits['review_repairs'] = self.review_repairs
        atomic_json(self.path, session)
        while True:
            record = session['attempts'][-1] if session['attempts'] else None
            if record and record.get('failure_kind') in ('REVIEW_PROTOCOL', 'ENVIRONMENT_PREREQUISITE', 'STAGNATION'):
                blockers = [f for f in record.get('assessment', {}).get('findings', [])
                            if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
                counter = record.get('no_progress') if record['failure_kind'] == 'STAGNATION' else None
                raise PlanError('Task planning needs intervention: ' + record['failure_kind']
                                + (f'; {describe_no_progress(counter)}' if counter else '')
                                + f"; report: {self.root / record['review']}",
                                [{'code': record['failure_kind'], **finding} for finding in blockers] or None)
            if record and record['status'] in ('CANDIDATE', 'VALID', 'REVIEWING', 'APPROVED'):
                candidate = (self.root / record['candidate']).read_text()
            else:
                semantic = bool(record and (record.get('review') or record.get('failure_kind') == 'REVIEW_UNCERTAIN'))
                used_semantic = sum(r.get('repair_kind') == 'SEMANTIC' for r in session['attempts'])
                used_structural = len(session['attempts']) - used_semantic
                if semantic and limits['review_repairs']:
                    if used_semantic >= limits['review_repairs']:
                        raise PlanError('Semantic plan repair exhausted')
                    repair_kind = 'SEMANTIC'
                else:
                    if used_structural >= limits['structural_candidates']:
                        raise PlanError('Structural plan repair exhausted: initial candidate plus two repairs')
                    repair_kind = 'STRUCTURAL'
                previous = None
                if record:
                    previous = {field: (self.root / record[field]).read_text()
                                for field in ('candidate', 'diagnostics', 'review') if field in record}
                    if not previous:
                        print('[task-plan] Previous invocation was reserved but no result was captured; retry consumes a new slot.', flush=True)
                    previous['history'] = self._history(session)
                record = {'attempt': len(session['attempts']) + 1, 'status': 'RESERVED', 'repair_kind': repair_kind}
                session['attempts'].append(record)
                atomic_json(self.path, session)
                self.budget()
                print(f"[task-plan] Generating candidate {record['attempt']} ({repair_kind.lower()} allowance)", flush=True)
                candidate = self.generate(record['attempt'], self.inputs, previous)
                self._save_evidence(session, record, 'candidate', candidate)
                record['status'] = 'CANDIDATE'
                atomic_json(self.path, session)
                self.budget()
            try:
                print(f"[task-plan] Validating candidate {record['attempt']}", flush=True)
                plan = parse_plan_output(candidate)
                self.validator(plan)
            except PlanError as exc:
                print(f"[task-plan] Rejected candidate: {json.dumps(exc.diagnostics)}", flush=True)
                self._save_evidence(session, record, 'diagnostics', json.dumps(exc.diagnostics, indent=2))
                record['status'] = 'REJECTED'
                atomic_json(self.path, session)
                continue
            if record['status'] == 'APPROVED':
                if self.structured_reviews:
                    history = self._history(session)[:-1]
                    result = validate_plan_review((self.root / record['review']).read_text(), plan,
                                                  self.inputs['context']['requirement_catalogue']['required_ids'], history,
                                                  self.inputs['context'].get('quality_contract'),
                                                  contract_defects=self.contract_defects(plan))
                    if result['verdict'] != 'APPROVED':
                        raise PlanError('Persisted candidate has no approving review')
                return plan
            # An uncertain review is retried within the same persisted bound,
            # rather than issuing an unbounded extra call after each crash.
            if record['status'] == 'REVIEWING':
                record['status'] = 'REJECTED'
                record['failure_kind'] = 'REVIEW_UNCERTAIN'
                atomic_json(self.path, session)
                continue
            record['status'] = 'REVIEWING'
            atomic_json(self.path, session)
            self.budget()
            print(f"[task-plan] Independent review of candidate {record['attempt']}", flush=True)
            history = self._history(session)
            if self.structured_reviews:
                report, assessment = self._structured_review(session, record, plan, history)
                record['assessment'] = assessment
                approved = assessment['verdict'] == 'APPROVED'
                blockers = [f for f in assessment['findings'] if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
                if any(f['category'] == 'ENVIRONMENT_PREREQUISITE' for f in blockers):
                    record['failure_kind'] = 'ENVIRONMENT_PREREQUISITE'
                # Stable IDs make three unchanged blocker sets a bounded stall, not another blind rewrite.
                signatures = [sorted(f['finding_id'] for f in h['assessment']['findings']
                                     if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING')
                              for h in history if 'assessment' in h]
                current = sorted(f['finding_id'] for f in blockers)
                counter = {'passes': no_progress_passes(signatures, current), 'limit': NO_PROGRESS_LIMIT,
                           'blocker_ids': current, 'attempt': record['attempt']}
                record['no_progress'] = counter
                session['no_progress'] = counter
                print(f'[task-plan] {describe_no_progress(counter)}.', flush=True)
                if counter['passes'] >= NO_PROGRESS_LIMIT:
                    record['failure_kind'] = 'STAGNATION'
            else:
                report = self.review(plan)
                approved = report.strip().endswith('TASK_PLAN_STATUS: APPROVED')
            self._save_evidence(session, record, 'review', report)
            record['status'] = 'APPROVED' if approved else 'REJECTED'
            atomic_json(self.path, session)
            if not approved:
                if record.get('failure_kind'):
                    print(f"[task-plan] Stopping: {record['failure_kind']}; no further candidate generation.", flush=True)
                else:
                    print(f"[task-plan] Candidate {record['attempt']}: semantic review requested changes; a retry requires remaining allowance and budget.", flush=True)
                log_retry_reason('task-plan', report, self.root / record['review'])
            self.budget()
            if approved:
                return plan

    def no_progress(self):
        """The persisted no-progress counter of this planning session, if a review recorded it."""
        return json.loads(self.path.read_text()).get('no_progress') if self.path.is_file() else None

    def _save_evidence(self, session, record, field, content):
        filename = f"attempt-{record['attempt']}-{field}.txt"
        path = self.root / filename
        if path.exists():
            raise PlanError('Refusing to overwrite planning evidence')
        path.write_text(content)
        record[field] = filename
        record[field + '_sha256'] = file_hash(path)
        atomic_json(self.path, session)
