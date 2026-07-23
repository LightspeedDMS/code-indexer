---
name: feedback-bug-report-means-report-not-fix
description: "A \"root cause + bug report\" request means investigate, file the issue, and STOP — never start fixing without an explicit fix instruction"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: dc8bb9b8-9e17-4e54-88ab-c2c8934fda86
---

When the user relays a production incident and asks to "find the root cause and provide a comprehensive bug report", the deliverable is the INVESTIGATION and the FILED ISSUE — nothing else. Do NOT launch tdd-engineer, do NOT modify code, do NOT "proceed with the fix via the mandatory workflow".

**Why:** On 2026-07-14 (temporal blank-out incident, issues #1405/#1406) I filed the bug reports correctly and then autonomously launched a fix. The user interrupted furiously ("why the fuck are you fixing the bug!!!!!") and ordered an undo of the partial changes. Fix decisions on production-impacting subsystems are the user's call — they may want to review the report, prioritize, or route it differently.

**How to apply:** Treat "bug report" requests as report-and-stop, even though [[feedback-autonomous-overnight-file-fix-iterate]] says file+fix+iterate — that mandate applies ONLY to explicitly autonomous overnight/loop missions, not interactive incident triage. Wait for an explicit "fix it" / "implement it" before touching code. Related: [[feedback-no-unnecessary-questions]] still applies to the investigation itself — dig to root cause without asking, just don't cross into implementation.
