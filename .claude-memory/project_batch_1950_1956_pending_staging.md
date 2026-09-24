---
name: project-batch-1950-1956-pending-staging
description: "Resume state for the v12.70.0 release: what is validated on staging, what is still open"
metadata:
  node_type: memory
  type: project
  originSessionId: 51a123fc-6e33-494f-85a8-3b7d83248eee
  modified: 2026-09-24T15:45:31.446Z
---

**PROMOTED. v12.72.0 IS LIVE IN PRODUCTION** (merge `92dadb1e`, 2026-09-24),
taking production from 12.65.0 across 62 commits and 7 version bumps. Authorized
by the operator with the full two-confirmation protocol; CI green on master.

Verified through the production front door after the auto-deploy: `12.72.0`,
`status: healthy`, 998 global repositories, a real `search_code` returning
results in 720ms under the `parallel` query strategy. The wildcard cap guard
also fired correctly (997 repos vs a cap of 50), so fleet-scale protections
work at real scale.

**Pre-flight checks worth repeating next promotion**: master-only NON-merge
commits was 0 (proves no un-back-merged hotfix would be reverted -- the ~23
master-only commits are all merge commits from prior promotions, which is the
normal shape and NOT divergence); and the merged tree was `git diff`-identical
to the staging tree the gates ran against.

**A real trap hit during the push**: production `check_health` reported
`active_jobs: 2` ninety seconds after reporting 0, which would have meant killing
in-flight work. `get_job_statistics` (the JobTracker, the authoritative registry
for restartable jobs) reported `active: 0, pending: 0` throughout, and health
oscillated back to 0. The two counters measure different things -- trust the
JobTracker for "is durable work at risk", not health's `system.active_jobs`.

**MCP surface always-on cost, measured at the front door at 12.71.0**: cluster
147 tools / 117,624 chars; solo 145 tools / 113,443 chars. The two-tool gap is
exactly the Langfuse `requires_config` gate (`start_trace`/`end_trace`), not a
deployment difference. `outputSchema` is confirmed absent from both payloads.

**P1 BUG BACKLOG IS DOWN TO ONE: #1956.** Closed this session with front-door
evidence on staging solo: #1922 (all 16 `Jsoup.*` facades report
`self_edge=false`; `reachable_to` reaches the facade from both
`HttpConnection.connect` overloads), #1952 (the true edge is restored -- 6 real
callers, `self_caller=false`), #1923 (two of three reproductions genuinely
fixed, the third was never a bug). `./rust-automation.sh` EXIT=0.

**#1952 and #1956 split cleanly and should stay split**: #1952 was "the real
edge is MISSING" (fixed); #1956 is "a false edge is PRESENT" (open). The decoy
still binds with an identical caller set and `self_caller=true` -- that is
#1956, not a #1952 regression.

**#1956's condition 3 is inert for a stronger reason than its record said.**
Measured: a class trips `has_unresolved_external_supertype` on its OWN DIRECT
external interface, before transitivity is consulted. Nearly every real Java
class directly implements some external interface, so any precondition phrased
as "is this hierarchy fully resolved" is inert on most code, not just deep
hierarchies. A curated JDK supertype table is the candidate remedy but was
REJECTED for now: it loosens a gate that controls candidate DELETION, and is
incomplete by construction. If ever pursued, land it on a TAG-ONLY path first.

**Do not "fix" an overload that looks wrong without checking assignability.**
`OVERLOAD_ARG_TYPE_MATCH` means arguments are COMPATIBLE with a signature, not
that Java would SELECT it; mutually applicable overloads all legitimately carry
it. Selection is JLS 15.12.2.5, which this binder does not implement -- now
#1967. See [[feedback_never_assert_unverified_facts_in_briefs]].

**`analyze_graph` findings can be TRUNCATED at 18** -- check `truncated` in the
envelope before concluding a symbol was not found. This nearly produced a false
"#1952 not reproducible" conclusion.

**A CHANGELOG correction shipped in 12.71.0**: the 12.70.0 entry filed Bug #1956
under "Fixed" and read as a completed fix. It is not one -- the narrowing pass
needs the calling type's ancestor chain fully resolved, and one unresolved
external supertype (a JDK interface such as `Cloneable`) defeats it for every
descendant, so it is inert on typical real-world Java. Corrected in place and
noted under 12.71.0. Never file a partial under "Fixed" in a public changelog.

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
