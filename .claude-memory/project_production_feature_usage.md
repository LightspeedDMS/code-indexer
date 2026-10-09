---
name: project-production-feature-usage
description: Which features matter most to the owner (workhorses vs rarely used) — drives prioritization and validation weight
metadata:
  type: project
---

Feature priority (owner, 2026-10-05):
- WORKHORSES (prioritize, validate thoroughly): X-Ray, regex search, semantic search, FTS, file listing and file reading.
- RARELY USED: SCIP (scip_* tools, SCIP REST/Web routes).

**Why:** owner statement while scoping a release; rarely used features seldom meet the P1/P2 bar.

**How to apply:**
- SCIP bugs/features default to low priority unless a security item meets the release bar.
- Validation weight goes to the workhorses through MCP and REST (incl. access checks); SCIP gets a smoke check at most.
- Release inclusion bar: see [[feedback-release-inclusion-bar]].
