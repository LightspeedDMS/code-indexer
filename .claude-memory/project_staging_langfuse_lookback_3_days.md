---
name: project-staging-langfuse-lookback-3-days
description: "Owner order (2026-10-06): the staging environment that pulls Langfuse traces must use a lookback of at most 3 days, set BEFORE old trace files are deleted and before provider keys return"
metadata:
  node_type: memory
  type: project
  originSessionId: 5d72bd7f-835f-4bea-a38d-c51aea1db2c4
  modified: 2026-10-06T20:03:36.455Z
---

Set `langfuse.pull_trace_age_days = 3` (default 30) on the staging environment that pulls Langfuse traces (the staging cluster; staging solo has no Langfuse repos). Staging is a test environment and does not need a long history. Apply it through the Web UI / REST config front door and confirm it reads back as 3.

**Why:** the Langfuse trace repo had grown to ~217k files (data back to July) because the sync never prunes files on disk; that size is what made the 2026-10-06 re-embed incident so expensive ([[project-reembed-cost-incident-2026-10-06]]).

**How to apply (order matters):**
1. Set the lookback to 3 first. The sync re-fetches any trace whose file is missing while it is inside the lookback window (`langfuse_trace_sync_service.py` ~620/632), so deleting old files under a 30-day window would bring them back.
2. Then back up and delete trace session folders whose newest file is older than 3 days, in every `langfuse_*` golden repo.
3. Only then install provider keys. Recorded as steps 2/2a of the gitignored staging bring-up checklist under `.analysis/` (environment detail stays out of git).
