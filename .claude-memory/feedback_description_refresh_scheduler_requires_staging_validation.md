---
name: feedback_description_refresh_scheduler_requires_staging_validation
description: description_refresh_scheduler.py changes require local AND staging testing with positive confirmation — mistakes risk runaway Claude processes burning money
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 873f73e7-fdb1-404d-b9f0-652abca0632f
---

Changes to `src/code_indexer/server/services/description_refresh_scheduler.py` (and any module that controls lifecycle/description backfill threading) require:

1. All three local test gates passing: `fast-automation.sh`, `server-fast-automation.sh`, `e2e-automation.sh`
2. Positive staging confirmation after deploy — manually verify the backfill threads start, complete, and log INFO (not ERROR) on rapid restarts

**Why:** Bugs in this module can trigger runaway Claude CLI subprocess invocations (each costing real money). The lifecycle_backfill and description_backfill threads invoke Claude to regenerate repository descriptions. A thread that crashes silently or re-registers incorrectly on restart can spawn duplicate Claude processes or loop indefinitely consuming significant API credits.

**How to apply:** Whenever touching `description_refresh_scheduler.py`, `lifecycle_batch_runner.py`, or any code that controls when/how backfill threads are launched — always run all three gates locally and verify on staging before calling it done.
