---
name: feedback_grep_absence_is_not_evidence
description: "A narrow grep returning zero is NOT proof a capability is missing - verify wiring with runtime introspection or a real call, never a single literal pattern"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 84868dd4-f5df-46fe-8bf0-3d4586c997c6
  modified: 2026-09-08T03:02:41.646Z
---

A grep that returns zero proves only that ONE spelling is absent. It never proves a capability is
missing. Three false negatives in a single session (2026-09-07, Epic #1786 / Story #1811), all the
same reasoning error:

1. `grep -c 'analyze_graph' src/code_indexer/server/mcp/tools.py` -> 0. I announced "this is the
   Epic #1786 unwired failure repeating". WRONG: `TOOL_REGISTRY` is built DYNAMICALLY from
   `tool_docs/*.md` via `ToolDocLoader`, so NO tool has a literal entry there - the long-working
   `xray_search` also returns 0. Runtime introspection showed 147 tools registered and dispatching
   correctly. The user confirmed: "we build the tool instructions dynamically".
2. `grep -c '_truncate_xray_result' handlers/xray_graph.py` -> 0. I reported the truncation fix as
   possibly not done. WRONG: graph mode legitimately uses its own `_truncate_graph_result`, sharing
   the same PayloadCache/cache_handle contract.
3. `grep -c 'to_thread.run_sync' handlers/xray.py` -> 0. I told the fixing agent the offload was
   still missing. WRONG: that file's established idiom is
   `loop.run_in_executor(xray_executor, ...)`; the work was already off the event loop.

**Why:** each time, I picked the spelling I EXPECTED the fix to use and read its absence as proof of
absence. Registration can be dynamic, data-driven, indirect, or use a different-but-equivalent
idiom. CLAUDE.md's own "mechanical check" (grep the core function names before closing an issue) is
a HEURISTIC that produces false alarms wherever wiring is indirect - it is good for catching
genuinely orphaned code, useless as proof of the reverse.

**How to apply:** to prove something IS wired, use a probe that cannot lie about it -
runtime introspection (import the registry, assert membership, print the resolved handler), a real
request through the front door, or an actual pipeline run. To prove something is NOT wired, search
idiom-agnostically (multiple spellings, the module name, the symbol imported under an alias) before
asserting absence. Phrase an unverified grep result as "my probe found nothing, verifying" - never
as "this is missing". Related: [[feedback_never_ship_unwired_work]] (the real rule, which this
heuristic serves), [[feedback_study_anomalies_deeply]], [[feedback_find_is_bfs_use_mmin]] (a
sibling case where the TOOL, not the pattern, silently produced a false negative).
