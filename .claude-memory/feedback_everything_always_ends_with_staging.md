---
name: feedback_everything_always_ends_with_staging
description: "Standing rule from the operator: every piece of work ends with staging validation through the front door -- green local gates are never the finish line"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-11T23:10:29.153Z
---

Operator's words, verbatim: **"everything. always. ends with staging"** (2026-09-11), said after
I closed three P2 defects on local gates alone because both staging doors were unreachable.

This is a STANDING rule, not a per-task instruction. Work is not finished when lint, fast,
server-fast, rust and e2e are all green. It is finished when the NEW capability or fix has been
driven through the REST/MCP front door **on staging** and real output has been observed.

**Why the operator insists:** the dev box is solo/SQLite. Whole classes of defect -- every
cluster-coordination bug, cross-node lease/snapshot contention, NFS behaviour, HAProxy routing,
PostgreSQL-backed shared state -- have a failure shape that does not exist locally. A local run
proves the mechanism is coherent; it is structurally silent on the thing that actually breaks in
production. A single-node PostgreSQL e2e phase is NOT two nodes contending.

**How to apply:**
- Plan the staging step into the work from the start; never treat it as an optional epilogue
  that gets dropped when it turns out to be inconvenient.
- If staging access is broken (MCP server down, OAuth expired, MFA/TOTP blocking headless REST),
  that is a BLOCKER to raise immediately and loudly -- not a reason to close on local evidence
  and note the gap. Ask for the OAuth authorization; the operator will provide it.
- Never write "validated"/"ready"/"complete" without naming the front-door call made, the real
  output it returned, and confirmation it exercised the NEW path rather than a neighbouring one
  that already worked.
- Closing an issue asserts completeness. Do not close on local gates alone unless the operator
  explicitly decides to accept that, and record the gap in the issue when they do.

Related: [[feedback_never_claim_ready_without_staging_e2e]] (same rule, stated as the project
DoD), [[project_verify_both_staging_environments]] (check BOTH clustered and solo staging),
[[project_local_server_solo_sqlite]] (why local validates only one branch),
[[reference_staging_totp_programmatic_auth]] (headless REST needs two-step TOTP; MCP is the
practical door for cluster work).
