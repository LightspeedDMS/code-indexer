---
name: Bug Package #469-472 Implementation Plan
description: Implementing bugs #469, #470, #471, #472 as single package on development branch, deploy to staging only, E2E regression test on staging
type: project
originSessionId: 8468be82-1a9f-4399-a331-e381cf4eee2c
---
Implementing 4 issues as a single package deployment:
- #469: Golden Repo Metadata Branch Contamination (P1) - 5 fixes, 2 already done (relative path + temporal mock)
- #470: Smart Embedding Cache via Content Hash Matching (P4 story)
- #471: --reconcile spawns per-file git diff subprocess (P2 bug)
- #472: Research Assistant CLI argument injection (P4 bug)

**Why:** User wants all fixes bundled, tested together, deployed as one unit to staging.

**How to apply:**
- Work on `development` branch only
- Do NOT push until ALL issues implemented, ALL tests pass, ALL automation green
- Single version bump + commit + push to development
- Merge to staging only (NOT master/production)
- E2E regression test on staging server after deployment
- Only after staging E2E passes is the package considered done
