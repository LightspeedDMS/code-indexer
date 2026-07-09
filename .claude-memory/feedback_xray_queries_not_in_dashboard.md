---
name: feedback_xray_queries_not_in_dashboard
description: xray_search and xray_search_batch calls must NOT appear in the dashboard — user has explicitly asked for this
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 873f73e7-fdb1-404d-b9f0-652abca0632f
---

xray query jobs (xray_search, xray_search_batch) must NOT appear in the CIDX server dashboard.

**Why:** The user explicitly requested this. Dashboard should show meaningful operational jobs (indexing, refresh, dep-map), not routine query calls which are high-frequency and clutter the view.

**How to apply:**
- When running test xray calls against production/staging for diagnostics, be aware they will show in the dashboard — warn the user first or avoid it.
- When implementing xray job handling, ensure xray_search / xray_search_batch jobs are excluded from the dashboard JobTracker visibility (this is a pending server-side configuration/implementation task).
- Do NOT add xray_search / xray_search_batch to dashboard-visible job types without confirming with the user.
