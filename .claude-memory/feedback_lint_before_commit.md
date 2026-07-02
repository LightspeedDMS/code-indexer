---
name: Run lint/format/mypy BEFORE git add, not via pre-commit
description: Always run ruff check, ruff format, mypy on changed files BEFORE staging. Pre-commit hook is safety net, not primary.
type: feedback
---

Run `ruff check --fix`, `ruff format`, and `mypy` on changed files BEFORE `git add`. Do NOT rely on the pre-commit hook to catch lint/format/type errors.

**Why:** Attempting to commit broken code wastes cycles -- each failed pre-commit hook requires re-staging, re-committing. The user explicitly said: "you should not commit or try to commit crap." Pre-commit hooks are the safety net, not the primary quality gate.

**How to apply:** Before any `git add && git commit`:
```bash
ruff check --fix src/file.py tests/file.py
ruff format src/file.py tests/file.py
mypy src/file.py
```
Then stage and commit. The pre-commit hook should always pass on first try.
