---
name: feedback-test-gate-must-check-pytest-exit
description: "A test-then-commit chain must gate on pytest's own exit code; `pytest ... | tail` returns tail's 0, so a typo'd path ('no tests ran') still commits"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 5d72bd7f-835f-4bea-a38d-c51aea1db2c4
  modified: 2026-10-11T04:00:32.657Z
---

When verifying a commit by running tests in an export and chaining the commit after it (`... && (cd export && pytest files | tail -2) && git commit ...`), the `&&` sees the exit code of `tail`, not pytest. On 2026-10-10 one test path was wrong (file lived under `services/`, not `storage/`), pytest printed "no tests ran" (exit 4) and the commit went through unverified; a re-run against the committed tree passed, so no harm, but the gate had been a no-op.

**Why:** a gate that cannot fail is not a gate; a mistyped path silently turns verification into a pass.

**How to apply:** redirect pytest to a file and capture `$?` directly (`pytest ... > out.txt 2>&1; rc=$?; tail out.txt; [ $rc -eq 0 ] && git commit ...`), or use `set -o pipefail`. Treat "no tests ran" / exit 4 / exit 5 as failure. Check every listed test path exists (`git cat-file -e HEAD:<path>` or `ls`) before trusting a count. Related: [[feedback-run-tests-with-timeout-and-monitor]].
