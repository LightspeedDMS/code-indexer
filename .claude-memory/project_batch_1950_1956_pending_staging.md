---
name: project-batch-1950-1956-pending-staging
description: "Resume state for the v12.70.0 release: what is validated on staging, what is still open"
metadata:
  node_type: memory
  type: project
  originSessionId: 51a123fc-6e33-494f-85a8-3b7d83248eee
  modified: 2026-09-24T15:45:31.446Z
---

v12.70.0 is committed on `development`, merged to `staging`, and RUNNING on BOTH
staging deployments (solo/SQLite and clustered/PostgreSQL, both confirmed
`version: 12.70.0` through their own front doors). Production is still 12.65.0.
Master untouched. No push-to-master authorization has been given.

**Validated through the front door on 2026-09-24** (this is real evidence, not
local gates):
- Storage-backend parity PROVEN: the same whole-graph evidence census on
  `jsoup-global` returns byte-identical numbers on solo/SQLite and
  cluster/PostgreSQL -- 6035 symbols, 48068 edges, every evidence-bit count
  equal. The graph engine is storage-backend independent.
- #1954 CLOSED with front-door evidence (solo): the Kotlin->Java call edge
  exists, the package-private target is not reported dead, and a separate
  `privateHelperNotVisibleToKotlin` correctly IS. Rust positive+negative control
  tests green.
- #1961 CLOSED: `analyze_graph.md` restructured, agent-visible cost 85,968 ->
  61,403 chars (-28.6%), binder internals split to `docs/xray-graph-binder-internals.md`.
  Committed as f7e2ca34.

**The measurement lesson from #1961, worth keeping**: a tool doc's frontmatter is
NOT all agent context. `tools/list` serves only `{name, description, inputSchema}`
(`server/mcp/tools.py` strips `outputSchema`), and `cidx_quick_reference(tool=...)`
serves the BODY alone with frontmatter stripped. For `analyze_graph`, 15,580 of
21,745 frontmatter chars reach no agent at all. Measure at the two front doors
before setting any doc size budget. See [[feedback_grep_absence_is_not_evidence]].

**Filed this session, all open, all priority-2**: #1962 (prose written into
`inputSchema` property descriptions costs every session ~23-32K tokens; three
measured trims worth ~8,500 chars), #1963 (`QUALIFIED_NAME` evidence bit never
set for a type-qualified static call -- proven by 20 qualified call sites
producing 0 such bits, reproduced on both deployments), #1964 (`/health`
permanently `degraded` from pre-#1950 restart artifacts still stored as
`status=failed`; #1950's write path itself verified WORKING, this is data
residue needing a backfill), #1965 (`xray-java-kotlin-fixture-global` holds
DIFFERENT content on cluster vs solo, invalidating cross-environment comparison).

**Still open and NOT fixed**: #1956 (P1, needs a per-call-site scope resolver --
substrate task, ten attempts failed), #1922/#1923 (P1; the bit census is direct
evidence #1923's complaint is still TRUE -- `OVERLOAD_ARG_TYPE_MATCH` fires on
72.7% of all edges and so cannot discriminate).

**Genuine staging failures needing triage** (not restart artifacts): solo has 5x
`typescript-global` indexing failures, 3x `homebrew-core-global` fetch, plus
single fetch failures on three more repos; the cluster has a repo whose semantic
indexing failed and four `index_cleanup` jobs failing against the CoW daemon
(HTTP 500 on DELETE, read timeouts).

Related: [[feedback_never_claim_ready_without_staging_e2e]],
[[project_verify_both_staging_environments]], [[feedback_no_half_wired_features]].
