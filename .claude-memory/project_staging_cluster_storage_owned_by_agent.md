---
name: project-staging-cluster-storage-owned-by-agent
description: "Staging cluster storage holds nothing of value to the owner; the developer agent owns it and may clean it up without asking"
metadata:
  type: project
  modified: 2026-09-27T19:00:00.000Z
---

Owner, 2026-09-27: "do the cleanup of the cluster storage. it's yours. there's nothing of value to
me, it's all for you as the developer agent, you own it."

**Why:** staging is a developer test bed. A full shared disk (from abandoned index build
artifacts) took down repository activation cluster-wide, and waiting for the owner to clear it
wasted time.

**How to apply:**
- When staging cluster storage fills or degrades, find the root cause and stop any refill first.
  Then clean up and restart the workers through systemd, without asking.
- Prefer the product's own front door or cleanup paths when removing whole repos or indexes, so
  the registry stays consistent.
- Keep a deletion log.
- This applies to staging only; production is never included.

Related: [[project_nfs_host_down_hangs_systemd]], [[reference_staging_nfs_wedge_recovery]],
[[feedback_check_running_jobs_before_restart]].
