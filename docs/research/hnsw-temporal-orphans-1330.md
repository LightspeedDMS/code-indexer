# Research Spike #1330 — HNSW Orphan Nodes on Temporal AND Regular Semantic Indexes

Status: RESEARCH FINDINGS — decision-ready, awaiting maintainer review. No implementation this session.
Scope: investigation only. No production code was changed, nothing committed. A throwaway repro harness
lives at `~/.tmp/story1330-hnsw-orphan-repro/repro.py` (outside the repo). A fully-spec'd follow-up
implementation-issue body is at `.tmp/hnsw_orphan_impl_issue.md`.

This document SUPERSEDES the earlier temporal-only draft of this file. The recommendation changed from
"single-thread the temporal build (F) + graded health check (D)" to **Strategy B (detect + repair at the
shared `HNSWIndexManager` level) + graded health check** once the scope expanded to regular semantic
indexes and the perf/mechanism numbers were measured. The reasons for the change are documented below.

---

## TL;DR

`check_hnsw_health` reports ORPHAN elements (HNSW nodes with **0 inbound connections**) on temporal
shards (e.g. `code-indexer-temporal-voyage_context_4-2025Q2`: "Element 270/271/272/273 has no inbound
connections (orphan)"), and the maintainer has **also directly observed orphans on REGULAR (non-temporal)
semantic indexes**. An orphan is unreachable in greedy ANN search unless it happens to be the entry
point, so that element's vector can silently fail to surface in queries — on the temporal feature AND on
regular semantic search, which is the core product ("query is everything").

Root cause is a property of the **shared HNSW build path**, not anything temporal-specific:

- Regular semantic, temporal, and multimodal indexes ALL converge on
  `FilesystemVectorStore.end_indexing()` → `HNSWIndexManager.rebuild_from_vectors()` →
  `build_hnsw_index_to_temp` (`hnsw_index_manager.py:544-555`): `init_index(M=16, ef_construction=200)`,
  `space="cosine"`, one **batched** `index.add_items(vectors, labels)` with **no `set_num_threads`
  anywhere** → hnswlib inserts using **all cores in parallel**. The builder is byte-identical for regular
  and temporal.

There are **two distinct orphan-producing regimes** (measured, see repro):

1. **Exact-tie RACE** — bit-identical vectors (cosine = 1.0). A multi-threaded `add_items` data race on
   the neighbor back-links leaves some tied nodes with zero inbound edges. **Non-deterministic**
   (run-to-run variance on identical input). `num_threads=1` **fixes it**. Bites as a small sparse count
   (0–2/run) from a handful of exact duplicates, or heavily only when a very large fraction (~100%) of
   the shard is bit-identical.
2. **Near-tie DETERMINISTIC** — vectors extremely close but NOT bit-identical (cosine > 0.9999999). The
   M-bounded `getNeighborsByHeuristic2` pruning genuinely discards a node from all its chosen neighbors'
   lists. **Deterministic** (single-thread == multi-thread). Bites from near-identical pockets as small
   as ~100 elements. `num_threads=1` does **NOT** fix it.

Realistic MIXED code corpora (mostly-diverse chunks with normal near-dup pockets — license headers,
boilerplate, 15%-overlap chunk chains) produced **0 orphans** in the repro. Orphaning requires either
near-global degeneracy (exact-tie regime) or a sizable (~100+ element) near-identical block (near-tie
regime). Real repos DO contain such blocks (large vendored/minified bundles, generated code, mass
copy-pasted boilerplate), which is the most probable source of the observed regular-index orphans.

**Revised recommendation: Strategy B — post-build orphan detect (via `check_integrity`) + a deterministic
repair pass, implemented once at the shared `HNSWIndexManager` finalize, plus graded health-check
severity (D).** It fixes BOTH regimes and BOTH index types, is perf-cheap (detection ~0.14% of build
time; repair only touches orphaned nodes), and is mechanism-agnostic.

Strategy F (single-thread the build) is **rejected as the shared fix** on two measured grounds: it is
**~4x slower** on large regular indexes (50k: 45.6s→181.7s; 200k: ~3.6min→~14.6min) AND it does **not**
fix the near-tie deterministic regime (ST == MT). Strategies A (tune M/ef) and C (shuffle insertion
order) are **rejected** — both measured to have zero effect.

---

## Shared build-path confirmation (the key finding)

All non-FTS vector index types finalize through ONE builder:

- `FilesystemVectorStore.end_indexing()` → `HNSWIndexManager.rebuild_from_vectors()` →
  `build_hnsw_index_to_temp` (`src/code_indexer/storage/hnsw_index_manager.py:544-555`):
  ```python
  index = hnswlib.Index(space=self.space, dim=self.vector_dim)   # space="cosine"
  index.init_index(max_elements=len(vectors), M=16, ef_construction=200,
                   allow_replace_deleted=True)
  labels = np.arange(len(vectors))
  index.add_items(vectors, labels)          # ONE batch, NO num_threads -> all-core parallel
  index.save_index(str(temp_file))          # atomic swap via rebuild_with_lock
  ```
- **Regular semantic, temporal, and multimodal all use this exact call** — parameters byte-identical for
  regular and temporal. No per-index-type M/ef override; no `set_num_threads()` anywhere in
  `hnsw_index_manager.py`.
- **The four HNSW add-sites** any thread/repair change must consider:
  `hnsw_index_manager.py:191` (build_index), `:555` (rebuild_from_vectors — finalize path), `:977` and
  `:985` (incremental single-point add / update). (`token_bucket` is unrelated; FTS/Tantivy is a
  separate index, unaffected.)
- **Regular-specific aggravators vs temporal:**
  - 15% chunk overlap (`src/code_indexer/indexing/fixed_size_chunker.py:49`, `OVERLAP_PERCENTAGE=0.15`)
    makes *adjacent* chunks of the same file near-duplicates by construction — a built-in source of
    near-tie pockets in every regular index (though the repro shows normal overlap chains, cosine
    0.16–0.99, do NOT orphan on their own).
  - Regular indexing has MORE `mark_deleted`/re-add churn than temporal (file edits re-index chunks vs
    immutable per-commit temporal points) — a separately-known HNSW degradation vector.
- **Today there is NO post-build integrity verification and NO orphan auto-remediation** anywhere.
  Orphans surface only via the operator-invoked `cidx health` exit code (`cli.py:8436`, exit 1 on
  `not valid`); MCP/REST/Web health endpoints are informational. No CI or startup gate.

Implication: because the trigger is the shared builder, a fix in `HNSWIndexManager` finalize covers ALL
HNSW index types at once. This makes a single shared-path fix both necessary (regular is affected) and
efficient (one place).

---

## The two-regime model (measured)

| Regime | Trigger | Determinism | `num_threads=1` fixes? | Onset |
|--------|---------|-------------|------------------------|-------|
| Exact-tie RACE | bit-identical vectors (cos = 1.0) | NON-deterministic (run-to-run variance on identical input+order) | YES | sparse (0–2) from a few exact dups; heavy only when ~100% of shard identical |
| Near-tie DETERMINISTIC | cos > 0.9999999, not bit-identical | Deterministic (single-thread == multi-thread) | NO | from near-identical pockets as small as ~100 elements |

Both regimes require abnormal similarity; neither fires on ordinary diverse content. The exact-tie race's
sparse magnitude (0–2/run) matches the temporal "3–4 tail orphans" observation; the near-tie
deterministic regime is the more likely source of regular-index orphans (a sizable near-identical code
block). A correct fix must handle BOTH, which single-threading cannot.

---

## Repro results (harness: `~/.tmp/story1330-hnsw-orphan-repro/repro.py`)

Uses the project's real custom hnswlib fork (`check_integrity()` present), M=16 / ef_construction=200 /
cosine, matching production; default all-core parallel `add_items` vs a single-thread control.

### Temporal shape (single-centroid / globally near-degenerate)
- Hypothesized band (cosine 0.94–0.99): **0 orphans** across M ∈ {8,16,32,48} × ef ∈ {100,200,400} ×
  size ∈ {270,1000,5000} × {clustered, uniform}. Tuning M/ef and shuffling order had **zero effect**.
- Exact-tie (noise=0): single-thread 0/10 seeds; multi-thread sparse non-deterministic (e.g.
  `[2,0,0,0,0,0,0,0]`) → race proven.
- Near-tie (noise 1e-6 / 1e-5): large orphan counts even single-threaded (~78–99 / 270) →
  deterministic; scales worse (n=1000 → 68%, n=5000 → 85%).

### Regular-semantic shape (realistic MIXED code corpus, 1024-dim voyage-code-3-like)
- Diverse majority + realistic near-dup pockets (license headers 30–100 copies, boilerplate
  getters/setters, near-identical fixtures, import runs, minified/vendored blobs, and 15%-overlap chunk
  chains spanning cosine 0.16–0.99): **0 orphans** at production M=16/ef=200, sizes 2k–5k, multi-thread.
- Orphaning requires **near-global degeneracy or a ~⅓-of-index near-identical block** (exact-tie regime)
  OR a **~100+ element near-identical pocket** (near-tie deterministic regime). Isolated small pockets
  inside an otherwise-diverse index did NOT orphan.
- Exact-tie pocket inside a diverse 3000-chunk index: orphans under multi-thread (race), **fixed by
  single-thread** — but only appears when the identical block is a large share of the index.
- Recall: at the 0-orphan configs, orphan-vs-control self-query had nothing to measure (control
  self-query hit rate 100%). Where orphans exist, an orphan is by definition unreachable by greedy
  descent, so its own vector's k-NN self-query can miss it — but because orphans sit at near-duplicate
  vectors, a near-twin surfaces near-identical content, so net *semantic* recall loss is smaller than the
  per-point miss suggests. Exact per-point recall loss on the near-tie regime remains to be measured on a
  REAL orphaned shard (see caveats).

### Benchmark — F (single-thread) cost vs B (detect/repair) cost
- Machine: multi-core (12 cores on the repro host); ratios are core-count dependent.
- `add_items` single-thread vs all-core: **~3.99x slower.** Measured 50k: 45.6s → 181.7s; extrapolated
  200k: ~3.6min → ~14.6min.
- `check_integrity()` scan (the detection cost of a repair pass): **~free — ~0.3s at 200k (~0.14% of
  build time).** Repair itself only touches the few orphaned nodes.
- Conclusion: F pays a ~4x build-time tax on the whole index AND still leaves near-tie deterministic
  orphans; B adds a ~0.14% detection scan + negligible per-orphan repair and fixes both regimes. A
  size-gate (single-thread small / repair large) is unnecessary — B dominates at all sizes.

---

## Per-index-type proneness and impact

| Index type | Build path | Orphan mechanism exposure | Real-world trigger likelihood | Impact of an orphan |
|------------|-----------|---------------------------|-------------------------------|---------------------|
| Regular semantic (code) | shared builder | BOTH regimes; +15% chunk-overlap near-dups; +more mark_deleted churn | Moderate — large vendored/minified/generated blocks, mass boilerplate, copy-pasted files → ~100+ near-identical pockets | **HIGH — silent recall loss on the CORE product (query is everything)** |
| Temporal (per-commit) | shared builder | BOTH regimes; near-identical commit embeddings within a shard | Low–moderate — bursts of trivial/near-identical commits (version bumps, merges, reverts) | Medium — a commit's diff silently missing from temporal queries; near-twin often surfaces similar content |
| Multimodal | shared builder | same as regular | data-dependent | same as regular |
| FTS / Tantivy | separate (not HNSW) | N/A | N/A | unaffected |

A single shared-path fix in `HNSWIndexManager` covers the top three rows.

---

## Strategy evaluation (revised)

| # | Strategy | Verdict | Measured basis |
|---|----------|---------|----------------|
| B | Post-build orphan detect (`check_integrity`) + deterministic repair, at shared `HNSWIndexManager` finalize | **ADOPT — primary shared fix** | Fixes BOTH regimes and ALL index types; detection ~0.14% of build; repair touches only orphaned nodes; mechanism-agnostic. |
| D | Graded health-check severity by orphan-ratio | **ADOPT — companion** | Cheap ergonomics; stop flipping `cidx health` exit code for benign low-ratio residual; expose `orphan_count`/`orphan_ratio`. |
| F | Single-thread the build (`num_threads=1`) | **REJECT as shared fix** | ~3.99x slower on large regular indexes AND does NOT fix the near-tie deterministic regime (ST == MT). Fails on both axes. |
| A | Raise M / ef_construction | **REJECT** | Zero measured effect on orphan rate in any band. |
| C | Shuffle / de-cluster insertion order | **REJECT** | Zero measured effect; near-tie regime is order-independent, race is not order-fixable. |
| E | Accept + document only | **PARTIAL** | Subsumed by D's documentation; doing nothing about the build leaves silent core-product recall loss — unacceptable given the maintainer observed it. |

Note on B's repair implementation: hnswlib's public API has no "add back-edge" primitive; a deterministic
repair that rewires each zero-inbound node into its nearest neighbor's link list (evicting the weakest
edge if the list is at M) most cleanly lives as a small C++ method on the already-customized fork
(consistent with how `check_integrity` was added). This is a real but bounded fork change and is the main
implementation cost of B — scoped in the follow-up issue.

---

## Recommended strategy (decision-ready)

**Strategy B + D at the shared `HNSWIndexManager` level.**

1. After every HNSW finalize (`rebuild_from_vectors` / `build_hnsw_index_to_temp`), run `check_integrity`
   (~0.14% of build time). If orphans are present, run a **deterministic repair pass** that, for each
   zero-inbound node, forces a back-edge from its nearest neighbor(s) (same heuristic, evicting the
   weakest edge if the neighbor is at M). Re-verify `valid` after repair. This fixes both the exact-tie
   race and the near-tie deterministic regime, for temporal AND regular AND multimodal indexes, in ONE
   place.
2. **Grade health-check severity** (D): expose `orphan_count` / `orphan_ratio`; classify OK / WARNING /
   ERROR by ratio+count; keep `cidx health` green for WARNING-level residual; keep hard corruption
   (invalid connections, unreadable/unloadable index) as ERROR.
3. **Validate on a REAL orphaned production shard** during implementation (the synthetic repro did NOT
   reproduce the 270–273 trailing-ID fingerprint) — confirm which regime the real orphans fall in and
   that repair drives them to zero.

Explicitly NOT recommended: F (single-thread — 4x slower and insufficient), A (M/ef tuning — no effect),
C (insertion-order shuffle — no effect). Recorded as out-of-scope with measured reasons so no effort is
wasted on them.

---

## Caveats (carry these into implementation — prominent)

1. **Synthetic vectors.** The repro used synthetic near-duplicate distributions; the real voyage-code-3
   embedding distribution may differ. Directional conclusions (which regime; F vs B; M/ef irrelevance)
   are robust, but exact thresholds are not production-calibrated.
2. **Real-world prevalence of the near-tie regime is UNKNOWN.** We proved it CAN orphan from ~100-element
   near-identical pockets; we have not measured how often real code indexes contain such pockets.
   Implementation must validate against a real orphaned shard.
3. **The 270–273 trailing-ID fingerprint was NOT reproduced** synthetically — orphans landed scattered
   mid-range. Root cause is strongly narrowed (shared builder, two regimes) but the exact fingerprint
   binding to a specific real shard is unconfirmed; the implementation's validation AC closes this.
4. **Repair needs a fork-level C++ method** (no public back-edge API). This is the primary implementation
   cost and risk of Strategy B; it must ship with the fork build + binding + CI gate
   (see `docs/hnswlib-custom-build.md`).
5. **Interruption is a red herring** — finalize is atomic (temp + `os.replace` under lock) and crash
   recovery exists; orphans are a property of the constructed in-memory graph, not a torn write.
6. **Recall loss is real but partially masked** by near-twins at near-duplicate vectors; exact per-point
   recall loss on the core product should be quantified on a real shard, not assumed negligible.

---

## Follow-up implementation story

A fully-spec'd implementation-issue body (for maintainer review, NOT to build this session) is at
`.tmp/hnsw_orphan_impl_issue.md`. Summary: detect+repair at `HNSWIndexManager` finalize (Strategy B),
graded health severity (D), applies to all HNSW index types, with a validation AC against a real orphaned
production shard, faithful real-hnswlib tests asserting orphan-rate → 0 and recall restoration, and
explicit out-of-scope for F/A/C with the measured reasons.

---

## Reproducibility / provenance

- Repro harness (throwaway, outside repo): `~/.tmp/story1330-hnsw-orphan-repro/repro.py`
  (raw output `run_output_final.txt` alongside).
- Shared builder verified by direct read: `src/code_indexer/storage/hnsw_index_manager.py:544-555`
  (M=16, ef=200, batched `add_items`, no `num_threads`); add-sites `:191, :555, :977, :985`.
- Chunk overlap: `src/code_indexer/indexing/fixed_size_chunker.py:49` (`OVERLAP_PERCENTAGE=0.15`).
- Integrity semantics: `third_party/hnswlib/python_bindings/bindings.cpp:725-810`.
- Health service / consumers: `src/code_indexer/services/hnsw_health_service.py:284-305`;
  `cli.py:8390-8522` (exit 1 at `:8436`); `server/mcp/handlers/repos.py:377-413`; REST
  `repository_health.py`, `activated_repos.py:314-324`.
- Temporal path: `temporal_indexer.py:691`; `filesystem_vector_store.py:646-724, 4158-4287`;
  `temporal_reconciliation.py:83-118`.
