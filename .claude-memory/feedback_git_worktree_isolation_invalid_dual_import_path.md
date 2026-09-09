---
name: feedback-git-worktree-isolation-invalid-dual-import-path
description: "git worktree-based commit isolation is structurally invalid in code-indexer -- the dual src.code_indexer/code_indexer editable-install import paths resolve to DIFFERENT trees inside a worktree"
metadata:
  type: feedback
  originSessionId: ca34043c-b05f-4314-8219-619a25ec9f26
  modified: 2026-08-25T18:13:53.180Z
---

`code_indexer` is installed editable, pinned to `/home/jsbattig/Dev/code-indexer/src`. Inside a `git worktree` checkout, the `src.code_indexer.*` import path resolves to files IN the worktree, but the bare `code_indexer.*` import path (which several modules, e.g. `telemetry/__init__.py`, use internally) still resolves back to the MAIN tree's editable install. Every worktree-based test run is therefore a silently mixed tree — some imports see the worktree's code, others see whatever is currently on `development` in the main checkout.

**Why**: Discovered during code review of #1676 AC7 (dead span-utility code removal). The reviewer tried to isolate the commit under review in a throwaway `git worktree` at the parent commit, expecting `import traced` to raise `ImportError` there (since the parent commit predates AC7's deletion). Instead got misleading phantom results (6 unrelated `ImportError: cannot import name 'traced'` failures) because the worktree's `src.code_indexer` and the main tree's `code_indexer` disagreed on what `spans.py` contained.

**How to apply**: Never use `git worktree` for commit-level test isolation in this specific repo (code-indexer). If you genuinely need to run tests against an isolated historical commit, use one of:
- `git show <commit>:<path> > /tmp/scratch_file.py` to extract just the specific file(s) needed, run tests against those in place (as several agents this session correctly did), OR
- A full standalone `git clone` to a separate directory with its OWN `pip install -e .` (a completely separate editable install, not sharing the main tree's site-packages entry) — expensive but genuinely isolated.
This is specific to editable-installed Python packages with this project's particular dual bare/`src.`-prefixed import pattern (a known recurring gotcha already noted elsewhere in this project's CLAUDE.md re: `src.`-prefixed import aliases resolving to distinct class objects) — worktree isolation is fine for repos without an editable install pinned to the main tree's `src/`.
