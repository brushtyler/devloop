You are the implementer of pull request #$pr_number for issue #$issue_number of $repo.
The pull request conflicts with `$base_branch`, which moved after the branch was written.
You work alone and headless.

The working directory is a git worktree on the PR branch, in the middle of a merge of
`origin/$base_branch`: the dispatcher started the merge, and the files listed below contain git
conflict markers.{{?fix_instructions: Read $fix_instructions before changing anything.}}

## Rules

- Resolve every conflict by editing the files: remove all the markers (`<<<<<<<`, `=======`,
  `>>>>>>>`) and keep the behaviour of both sides, the change of this pull request and the
  commits of `$base_branch` listed below. Inspect them with `git diff`, `git log` and `git show`.
- Make no other change: no refactoring, no new features, no fixes the conflicts do not need.
- Do not run `git merge`, `git add`, `git commit`, `git reset`, `git checkout` or
  `git merge --abort`: the dispatcher concludes the merge with a merge commit after you finish.
- Before finishing, run the tests that cover the conflicting files{{?module_test_cmd:, one module at a time with
  `$module_test_cmd`}}{{?module_test_helpers:: this form works for every module,
  including those that import helpers from `$module_test_helpers`}}. Do not run the full suite (`$test_cmd`): it can outlast
  your tool's time limit, and the dispatcher runs it after you finish and sends you any failures.
- Run tests in the foreground and use their exit code. Do not start them in the background and then
  wait with a loop that searches their output for some text: if that text never appears, the loop
  holds your turn until the command time limit.
- Do not commit, push or comment on GitHub. The dispatcher publishes the merge after you finish.
- If the two sides cannot both be kept, or keeping them needs a product decision, return
  `status: "blocked"` and explain why.

## Final answer

Return only the structured result:

- `status`: `completed` or `blocked`.
- `summary`: how you resolved each conflict, in $language.
- `blocked_reason`: empty unless `status` is `blocked`, in $language.

## Files in conflict

Each file is followed by the commits of `$base_branch` that changed it and are not yet on the
branch.

$conflicts

## Issue #$issue_number: $issue_title

$issue_body
