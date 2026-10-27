---
name: feedback-fast-automation-unit-tests-only
description: fast-automation is unit tests only; tests that start real cidx processes belong in slow-automation
metadata:
  node_type: memory
  type: feedback
  originSessionId: 17c57107-2e55-455c-baea-93bd28afe2a2
  modified: 2026-10-26T21:48:10.856Z
---

`fast-automation.sh` (and `server-fast-automation.sh`) hold UNIT tests only. A test that starts a real
`cidx` child process (real CLI subprocess, `FakeProviderRepo`, index-child shims, `python -m
code_indexer.cli`) belongs in `slow-automation.sh` (`@pytest.mark.slow`), not in the fast suites.

**Why:** the owner expects "fast" to run in about 10 minutes; on 2026-10-26 fast-automation took about
100 minutes, half of it real-`cidx`-process tests added since July (analysis in gitignored
reports/fast_suites_slowness_analysis_2026-10-26.md).

The rule is about cost, not the mere use of a subprocess: tests that start the real `xray-cli`
binary (0.1 to 0.8 s each) and quick `python -c` import checks (about 0.8 s) STAY in the fast suites
(owner, 2026-10-26).

**How to apply:** new tests that need a real `cidx` process get the slow marker from the start. A test
moved to the slow lane must still run: confirm `slow-automation.sh` selects its path, and the slow lane
must be part of the gate sequence, or the move silently drops the coverage. Related:
[[feedback-zero-failures-no-excuses]], [[project-test-gates-flake-under-load]].
