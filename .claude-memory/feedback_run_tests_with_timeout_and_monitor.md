---
name: feedback_run_tests_with_timeout_and_monitor
description: NEVER launch tests blindly without timeout and monitoring — know expected duration before running
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 541b798d-59f6-4161-bb2e-134300c6b365
---

NEVER launch a test suite and passively wait without knowing expected duration and actively monitoring progress.

**Why:** `tests/unit/server/routes/test_xray_routes.py` hung for 70+ minutes wall-clock (only 1:49 CPU) while I sat in a lawn chair waiting for a completion notification that never came. Completely unacceptable waste of time.

**How to apply:**
- Always run pytest with `--timeout=<N>` (e.g. `--timeout=30` for unit tests, `--timeout=60` for integration tests) so individual tests are killed if they block
- Always use `timeout <seconds>` wrapper on the outer command too (belt + suspenders)
- NEVER use `| tail -30` when running tests in background — it swallows intermediate output; use direct output to a file with `tee` or run foreground
- Known expected durations: `fast-automation.sh` ≤ 10 min, `server-fast-automation.sh` ≤ 15 min, individual unit test files ≤ 30 seconds
- When a test takes more than 2x expected duration, STOP and investigate — do not wait
- Run foreground with explicit timeout: `PYTHONPATH=./src timeout 300 python3 -m pytest <path> -v --tb=short --timeout=30`
- Monitor foreground output live — you can see which test is currently running

**Reference:** `tests/unit/server/routes/test_xray_routes.py` — confirmed hang (exit 143 within 30s wall timeout)
