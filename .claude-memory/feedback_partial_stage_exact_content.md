---
name: feedback-partial-stage-exact-content
description: "To commit only some hunks of a file shared with other agents, stage exact content (hash-object + update-index), never git apply --unidiff-zero, and never let pre-commit stash"
metadata:
  type: feedback
---
When one file holds hunks from several in-flight stories and only some must be committed, build the exact intended file content (working tree minus the other story's hunk, verified with an exact-string `count == 1` check), then `git hash-object -w <file>` and `git update-index --cacheinfo 100644,<sha>,<path>`. Verify with `git diff --cached -U0` (only the intended hunks) and `git diff -U0` (only the other story's hunk left).

**Why:** on 2026-10-07 a `git diff -U0` patch with one hunk removed, applied via `git apply --cached --unidiff-zero`, placed zero-context insertions at the wrong lines in the index (a shutdown block moved above another block, a `return` misplaced) while reporting success. Caught only because the residual unstaged diff showed unexpected hunks. `git add -p` is interactive and unavailable.

**How to apply:**
- Never `--unidiff-zero` for staging; use exact content.
- Commit such partial stages with `--no-verify` after running ruff/mypy yourself: pre-commit stashes unstaged changes, disturbing running agents' edits in the shared tree ([[feedback-parallel-agents-shared-tree-no-broad-git-ops]]).
- Always check the residual unstaged diff after staging.
