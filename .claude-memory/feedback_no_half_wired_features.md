---
name: feedback_no_half_wired_features
description: "Never ship a feature's write/relocation half without its read half — shipping data to a new location with no reader is a silent correctness break"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

When a feature MOVES or TRANSFORMS where data lives (e.g. Story #1457 relocating temporal shards from in-repo to a golden-owned sister location), the WRITE side and the READ side must ship together. Shipping the write side live while the read side still looks at the old location is a silent data-loss-class bug: the data is safely written to the new place, but every query returns zero as if it never existed — no error, no log.

**Why:** The user reacted very strongly ("completely bullshit") to discovering #1482 — Story #1457's relocation-to-sister-location (write) was armed on every server via `CIDX_SERVER_REFRESH_CONTEXT=1`, but the live temporal read path (`temporal_worker.py` → `execute_temporal_query_with_fusion`, the Story #1400 replacement path) never got the `TemporalShardResolver` wiring, so temporal queries silently returned 0 on any local-disk server (= production). The resolver WAS wired, but into the now-unused `SemanticQueryManager._execute_temporal_query`. A gate/env flag that changes WHERE data is written must be matched by a reader that looks THERE.

**How to apply:**
- When reviewing or building any feature that relocates/renames/re-layouts data, trace BOTH the write and every live read/status path to the same location before declaring it done. Prove a real end-to-end round-trip (write to new location, then read it back via the actual front door).
- Be suspicious of "the resolver/reader is wired" claims — verify it's wired into the path the front door ACTUALLY calls today, not a superseded one (paths get replaced; wiring can land in a dead branch).
- A status/existence detector counts as a read path too — fix it alongside the query path or status lies.
- Relates to [[feedback_study_anomalies_deeply]] (a query returning 0 is an anomaly to root-cause, not dismiss) and [[feedback_no_fallbacks_ever]] (fail loud, don't silently read the wrong/empty location).
