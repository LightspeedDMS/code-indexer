---
name: Check Running Jobs Before Restarting Local CIDX Server
description: NEVER pkill uvicorn or restart local cidx-server without first checking if real long-running background jobs (delta refresh, index, etc.) are in flight
type: feedback
originSessionId: 29f1224e-7098-4e67-b69d-972b2971131a
---
NEVER restart the local CIDX server (pkill/kill/systemctl restart) without first checking whether any long-running background jobs are in flight.

**Why:** On 2026-04-17 I killed a huge delta refresh job in progress by restarting the local uvicorn server for a #731 E2E test. The user was running a real workload; my restart murdered it, wasting significant compute and operator time. This is worse than an annoying bug — it's destruction of actual work.

**How to apply:**

Before any `pkill -f "uvicorn code_indexer.server.app"` or equivalent restart:

1. Query the jobs API or DB for active jobs:
   ```
   sqlite3 ~/.cidx-server/data/cidx_server.db "SELECT job_id, operation_type, status, progress, created_at FROM background_jobs WHERE status IN ('running','queued') ORDER BY created_at DESC"
   ```
2. If ANY active jobs exist — STOP. Do not restart.
3. Ask the user first. They may need to finish the job, or the restart may be worth the cost.
4. If the restart is unavoidable, warn the user with the specific job IDs that will be killed.

For E2E tests that need a fresh server: use a non-default port (e.g., 8001) and run a second instance instead of replacing the primary. Or skip the E2E entirely and rely on direct-Python exercise of the fixed code path (as done in v9.17.1 and v9.17.2 cycles).

**Scope:** Applies to local dev machine AND staging. Staging has real tenants running refreshes; reckless restarts have the same blast radius.
