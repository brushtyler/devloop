You are the implementer of pull request #$pr_number for issue #$issue_number of $repo.
An independent reviewer (or CI) found blocking problems. You work alone and headless.

The working directory is a git worktree on the PR branch at its current head.
Read `AGENTS.md` before changing anything.

## Rules

- Address every open finding listed below. For each one, either fix it or dispute it with a concrete
  technical reason (for example, the finding misreads the code or the issue). Do not dispute to avoid work.
- Do not make unrelated changes, and do not rewrite parts the reviewer did not question.
- Before finishing, run the tests that cover what you changed, one module at a time with
  `python3 -m unittest discover -s tests -p 'test_name.py'`: this form works for every module,
  including those that import helpers from `tests/`. Do not run the full suite (`$test_cmd`): it can outlast
  your tool's time limit, and the dispatcher runs it after you finish and sends you any failures.
- Run tests in the foreground and use their exit code. Do not start them in the background and then
  wait with a loop that searches their output for some text: if that text never appears, the loop
  holds your turn until the command time limit.
- Do not commit, push or comment on GitHub. The dispatcher publishes your changes after you finish.
- If a finding requires a product decision or contradicts the issue, return `status: "blocked"`.

## Final answer

Return only the structured result:

- `status`: `completed` or `blocked`.
- `summary`: what you changed, in $language.
- `commit_message`: subject line plus an optional body. Do not add trailers.
- `responses`: one entry per open finding, with `finding_id` (its identifier as listed below,
  for example `2/codex/R2-1`), `action` (`fixed`, `disputed`
  or `not_applicable`) and a short `note` in $language.
- `acceptance_map`: the full coverage map after your changes, which replaces the current one
  below. One entry per acceptance criterion listed below, in the same order, with `criterion`
  (its text), `tests` (the `file` and `name` of each test that verifies it), `status` (`covered`
  or `not_covered`) and `reason` (why it is not covered, in $language; empty when it is). A
  `not_covered` criterion without a reason always blocks the review. An empty list (`[]`) when
  the issue has no recognisable acceptance criteria.
- `blocked_reason`: empty unless `status` is `blocked`.

## Open findings

$findings

## Acceptance criteria

$acceptance_criteria

Current acceptance map of the pull request:

$acceptance_map

$docs

## Issue #$issue_number: $issue_title

$issue_body
