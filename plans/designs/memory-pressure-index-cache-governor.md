# Memory-Pressure-Aware Index-Cache Governor

**Status:** Design (architect-reviewed) — pending epic/story creation
**Author:** Claude (design-story-spec workshop, agentic) + software-architect review
**Date:** 2026-06-24

## 1. Problem & root cause

Temporal queries fan out over time-sharded HNSW indexes. `temporal_fusion_dispatch.py:371` scans a provider's quarterly shards **sequentially**, and `:396-408` (Bug #1171) **unconditionally evicts** each shard's HNSW from the cache in a `finally` after every shard. Net effect: every temporal query reloads every shard from disk (NFS in cluster), with **zero cross-query reuse**.

Measured (staging, `typer-global`, warm): ~6.4s @ limit-10, ~11.7s @ limit-40; the Voyage rerank call itself is ~0.4s (negligible). The slowness is the sequential reload, not the reranker.

**Bug #1171's trigger was a real production SWAP death-spiral.** The per-shard evict guarantees resident HNSW ≈ one shard so the cache cannot balloon RSS. The RAM-safety is therefore a HARD requirement; we cannot simply remove the evict. The defect is that the safety is paid **unconditionally**, even with gigabytes of headroom.

## 2. Goal / non-goals

**Goal:** Retain shard HNSWs across queries when memory is comfortable (fast warm temporal queries), and fall back to the proven #1171 evict-after-use behavior only under real memory pressure — driven by a centralized, node-aware, configurable governor. Never swap.

**Non-goals (this epic):** Parallelizing the sequential shard scan (separate latency work; composes later but raises peak RAM so must come after the governor bounds it). Changing the indexing/sharding layout.

## 3. Design

### 3.1 Pressure signal (node-level, cgroup-aware)

The swap spiral is a **node-level** failure, so the band is computed from a node-level signal shared identically by all workers (each reads the same `/proc` + cgroup). The **action** (evict) is necessarily per-process.

```
effective_limit = min(host_total, cgroup_limit_or_inf)
effective_used  = cgroup_current  if cgroup-limited  else host_used
used_pct        = 100 * effective_used / effective_limit
```

- cgroup v2: `/sys/fs/cgroup/memory.max` (`"max"` ⇒ unlimited) + `memory.current`.
- cgroup v1: `memory.limit_in_bytes` (huge sentinel ⇒ unlimited) + `memory.usage_in_bytes`.
- No/unlimited cgroup: `psutil.virtual_memory()` (`.total`/`.used`).
- Detect once (v2 → v1 → host), cache the resolved reader for process lifetime.

This makes the user's intuition correct in **both** worlds: 15% RED on a bare 48GB box ≈ 7.2GB; 15% in a 4GB container ≈ 600MB — both "15% of what this process is actually allowed."

**Swap is a separate override trigger:** any swap-IN activity (`/proc/vmstat pswpin` delta > 0 between samples) forces RED immediately, regardless of `used_pct`. Measure the *rate* (delta per interval), not the sticky absolute `swap.used`. (Swap-out alone can be benign; swap-IN is the death-spiral signature.)

### 3.2 Band state machine (hysteresis + dwell)

| Band | Enter | Exit (hysteresis) | Action |
|------|-------|-------------------|--------|
| GREEN (retain) | `used_pct < yellow` | — | retain shards; no post-shard evict → cross-query reuse |
| YELLOW (degrade) | `used_pct >= yellow` (def 70) | → GREEN only when `used_pct < yellow - gap` (def 60) | sampler evicts LRU to a floor + `malloc_trim`; reduce rerank overfetch; hottest entries still retained |
| RED (safe) | `used_pct >= red` (def 85) OR `pswpin_rate > 0` | → YELLOW only when `used_pct < red - gap` (75) AND no swap AND dwell ≥ `red_min_dwell_seconds` (30) | exact #1171: evict-after-use per shard + trim + **in-flight shard loads capped at 1** |

- No GREEN↔RED direct edges — transitions step through YELLOW (one band of graceful degradation; avoids a single-hop cliff).
- Band recomputed by ONE sampler thread per process on `sample_interval_seconds` (def 2.0); the query path reads the current band atomically (never polls psutil/cgroup per shard).
- **Fail-safe default = RED** on any signal error / pre-init (Anti-Fallback: never silently revert to unbounded retain).

### 3.3 Config (cluster-aware, Web-UI-tunable, NO env vars)

New `CacheConfig` fields (sibling to `index_cache_max_size_mb`), mapped in `_update_server_setting` and surfaced in `config_section.html`; read **live each decision** (hot-reload, mirrors `coalesce_enabled`) — which is exactly what the E2E harness needs to flip bands without restart:

```
memory_governor_enabled: bool = True            # False ⇒ revert to #1171 evict-after-use (SAFE), NOT retain
memory_governor_yellow_pct: float = 70.0
memory_governor_red_pct: float = 85.0
memory_governor_hysteresis_pct: float = 10.0
memory_governor_red_min_dwell_seconds: int = 30
memory_governor_sample_interval_seconds: float = 2.0
memory_governor_swap_forces_red: bool = True
memory_governor_rss_inflation_factor: float = 2.0   # fixes file-size undercount for the LRU cap only
```

Validation: `0<yellow<red<=100`, `hysteresis < min(yellow, 100-red)`; reject loudly otherwise. The Web UI shows these on the Config screen so an operator can set e.g. yellow=10/red=15 on this 48GB box to force band transitions for testing.

### 3.4 Integration

A separate injected `MemoryGovernor` collaborator (NOT baked into `HNSWIndexCache`, NOT a `get_or_load` wrapper — retention is decided post-use and by the sampler, not at load time). Both HNSW and FTS caches consult it. Built in `service_init.py` right after `initialize_caches(worker_count)`; absent on CLI/solo (`hnsw_index_cache=None`) exactly like the coalescer registry.

Replace `temporal_fusion_dispatch.py:396-408` with a governor consult:

```python
finally:
    cache = getattr(vector_store, "hnsw_index_cache", None)
    gov = getattr(vector_store, "memory_governor", None)
    if cache is not None and (gov is None or gov.should_evict_after_shard()):
        cache.invalidate(str((Path(vector_store.base_path)/shard_name).resolve()))
        if gov is not None: gov.maybe_trim()
```

`should_evict_after_shard()` → True iff band==RED OR disabled OR signal-init-failed. So: CLI/solo unchanged; server GREEN retains; server RED/disabled/errored = identical to #1171. The RED concurrency=1 gate lives in the dispatch loop (today's scan is already sequential; the gate makes it enforced, defending a future parallelization).

### 3.5 Observability (for ops AND E2E assertions)

Dedicated admin endpoint `GET /api/admin/memory-governor` (+ MCP twin for front-door E2E), NOT overloaded onto health/node-metrics (stable contract; E2E needs on-demand reads). Returns: `band`, `used_pct`, `effective_limit_mb`/`effective_used_mb`, `basis` (`cgroup_v2|cgroup_v1|host`), `pswpin_rate`, `swap_used_mb`, transition counters (`green_to_yellow`, `yellow_to_red`, `red_to_yellow`, `yellow_to_green`), action counters (`shards_evicted_after_use`, `lru_evictions`, `trim_calls`), `enabled` + echoed watermarks, and `pid` (multi-worker assertions). Cache `get_stats()` (hit/miss/eviction) proves GREEN-retains (warm hit delta) vs RED-evict (eviction per shard, zero cross-query hits).

**Durable structured logging (evictions must be logged "somewhere" — front-door queryable).** Beyond in-memory counters, the governor emits a structured record to the server log store (`~/.cidx-server/logs.db`, queryable via the `admin_logs_query` MCP front door AND `sqlite3`) for every band transition and every eviction action, with stable codes:
- `GOV-001` band transition (old→new, `used_pct`, `basis`, `pid`)
- `GOV-002` RED evict-after-use (shard, est. freed MB)
- `GOV-003` YELLOW proactive LRU evict (count, est. freed MB)
- `GOV-004` `malloc_trim` (released bool)
- `GOV-005` swap-in detected → forced RED (`pswpin_rate`)
So evictions are positively observable two independent ways: front-door log query + endpoint counters. (Rate-limit GOV-002 like the HNSW-stale logger to avoid storms under sustained RED.)

## 4. E2E test strategy (local pressure generation)

**Primary — config-injected watermarks (deterministic, CI-safe):** with the box at ~30% used, inject `yellow=10/red=15` (hot-reloaded) → forces RED with no real memory touched; inject `yellow=95/red=98` → forces GREEN at the same real usage. Drives all three bands by config alone.

**Secondary — bounded balloon (the swap=0 proof):** a child process allocates-and-touches anon pages up to just under the configured RED line; assert the band flips to RED and `pswpin` delta stays 0 throughout. Opt-in gated (`CIDX_PERF_TEST=1`), target well below true OOM, torn down in `finally`.

**Prerequisite fixture:** local dev has no temporal-enabled golden repo, so the harness must first build a **small multi-shard temporal index** (commits across ≥2 quarters ⇒ ≥2 time-shards) via the front door, or cross-shard reuse vs evict-after-use can't be exercised.

| Assertion | Observable |
|---|---|
| GREEN retains | 2nd temporal query: HNSW `hit_count` += #shards, `eviction_count` flat; latency markedly lower (warm) |
| YELLOW degrades | `lru_evictions`/`trim_calls` increment; overfetch reduced; band=YELLOW |
| RED evict-after-use | `shards_evicted_after_use` += #shards/query; zero cross-query hits; band=RED |
| Hysteresis (no flap) | oscillate injected `used_pct` across the gap; counters increment exactly once per real crossing |
| NO swap (hard guarantee) | `pswpin` delta == 0 across the balloon-driven RED test |
| cgroup correctness | `basis` == cgroup_v2/v1 in a memory-limited container; math vs container limit |
| Workers agree | multi-worker: every worker reports the same `band` for the same node state |

### 4.1 Dual-environment validation plan (solo + cluster) — MANDATORY

Both deployment modes MUST be validated by manipulating the watermark knobs against **large, sizeable indexes**, with positive **logged** eviction evidence and an observed memory curve as the limit is pushed. No declaring done without: the `GOV-*` log lines, the governor-endpoint band/counter timeline, and a `used_pct`/RSS-vs-time curve showing the governor holding the line with swap flat.

**Large-index requirement.** Toy repos (docopt) produce tiny HNSW that won't move RSS measurably. Use indexes *known to be large*:
- Solo: build a temporal index on a large, long-history repo (so individual quarterly shards are sizeable) and/or register several large non-temporal golden repos. Record each index's on-disk size AND measured-RSS-on-load first — this doubles as the `rss_inflation_factor` calibration.
- Cluster (staging): use the largest existing temporal/golden repos already present (e.g. `fastapi`, `pydantic`, `starlette`, `rich`, `httpx`; `typer` is temporal-enabled). Add more if RSS movement is insufficient.

**Solo (local, non-clustered, `:8000`, SQLite) — keep the dev server running, do not kill it:**
1. Build/register large index(es); record baseline `used_pct` + RSS.
2. Via the Web UI Config screen, set watermarks (e.g. `yellow=baseline+2`, `red=baseline+5`) to simulate "near the limit" without real OOM; and separately raise them high to force GREEN.
3. Drive repeated temporal/semantic queries on the large index; observe via the endpoint AND `admin_logs_query` / `sqlite3 ~/.cidx-server/logs.db "SELECT ... WHERE message LIKE 'GOV-%'"`:
   - GREEN: warm cache hits, NO `GOV-002/003`, RSS rises then plateaus (retained).
   - YELLOW: `GOV-003/004` appear, RSS held below the line, overfetch reduced.
   - RED: `GOV-002` per shard, RSS bounded to ≈ one shard, zero cross-query hits.
4. Balloon-push: raise real `used_pct` with a bounded balloon toward `red`; confirm band flips to RED BEFORE any swap-in (`pswpin` delta == 0; `GOV-005` only if swap is ever touched). Capture the `used_pct`/RSS/band curve.

**Cluster (staging `linner.ddns.net`, PostgreSQL, `uvicorn --workers N` × 3 nodes behind HAProxy):**
1. Use the largest temporal/golden repos present.
2. Set watermarks via the Web UI (config persists in shared PG → all nodes pick it up live); confirm the knob change propagates cluster-wide.
3. Drive queries through HAProxy (round-robin → multiple nodes/workers) and observe:
   - **Per-node/per-worker band AGREEMENT** for the same node memory (endpoint `pid` + `basis`; each node computes its own node-level signal).
   - **Eviction logs in EACH node's store** (`GOV-002/003`) — query per node (a single `admin_logs_query` hits one node via affinity; cover all nodes via repeated/targeted queries or the per-`pid` endpoint).
   - **cgroup `basis` correctness** if staging nodes are containerized (`basis == cgroup_v2/v1`; watermark math vs the container limit, not host).
   - Memory behavior per node as large shards load under low watermarks; **no node swaps**.
4. Validate the cluster config path end-to-end: set `red` low in the Web UI → ALL nodes enter RED → ALL emit `GOV-002` (proves shared-config propagation + per-node local action).

Note: cluster validation runs on **staging** (allowed), never production without explicit authorization.

### E2E feasibility (Step 7) — no blocker
- Build/run: `PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app --port 8000` (already running locally).
- Creds: local admin `admin/admin`; `E2E_VOYAGE_API_KEY` in `.local-testing` (for building the temporal index).
- Dependencies: none external beyond VoyageAI; psutil present; cgroup test needs a memory-limited container (optional, gated).
- Only setup cost: build a small temporal index locally. Feasible.

## 5. Key decisions & rationale
- **Node-level signal, per-process action** — swap is node-level; per-process RSS banding can't see a node spiral.
- **cgroup-aware basis** — psutil reports host RAM in containers; without cgroup the % is silently wrong in the cluster deployment that matters most.
- **Never band on file-size accounting** — `index_size_bytes = index_file_size() + sys.getsizeof(id_mapping)` undercounts true RSS (shallow dict size; on-disk vs in-RAM); this is why the static cap couldn't prevent the swap. Inflation factor only patches the secondary LRU cap.
- **malloc_trim is best-effort** — real RSS drop comes from `del entry` releasing hnswlib's mmap; trim can be a no-op. Never assert trim lowered RSS; re-measure after eviction.
- **Hysteresis + YELLOW + RED min-dwell** — prevents both band flapping and the evict/reload workload cliff. Under sustained real pressure, temporal queries SHOULD be slow (correctness > latency).
- **Fail-safe = RED; kill-switch = #1171-safe** — disabling/erroring must never be the unsafe (retain-everything) direction.

## 6. Scope — EPIC of 4 stories (sequenced 1→2→3→4; 1&2 parallel)
1. **Governor core + signal layer** (no behavior change): `MemoryGovernor`, cgroup v1/v2+host detection, `used_pct`/`pswpin` reader, band state machine + hysteresis + dwell, sampler thread, counters, inflation-factor fix. Wired but consulted by nothing. Unit-tested with injected fake memory readers (cgroup math provable without a container).
2. **Config + Web UI**: the 8 `CacheConfig` fields, `_update_server_setting` mapping, validation, `config_section.html`, live hot-reload. Low-risk, independently shippable.
3. **Call-site integration (the behavior change, SAFETY-SENSITIVE — isolated commit per Story #929)**: replace `temporal_fusion_dispatch.py:396-408` with the governor consult; RED concurrency=1 gate; inject governor onto HNSW + FTS caches; YELLOW proactive eviction + overfetch cut; fail-safe-to-RED explicitly tested. Requires `server-fast-automation.sh`.
4. **Observability endpoint + E2E harness + dual-environment validation**: admin endpoint + MCP twin; `GOV-*` structured eviction/transition logging to the server log store (front-door queryable); temporal-index builder fixture (large, multi-shard); config-injection band-transition tests; gated balloon swap=0 test; AND the mandatory **solo + cluster** validation plan from §4.1 — knob manipulation against large indexes with logged-eviction evidence and observed memory curves in BOTH local (non-clustered) and staging (clustered). Final regression gate.

## 7. Open items for implementers
- Empirically calibrate `rss_inflation_factor` (load a known index, diff process RSS) before trusting the LRU cap.
- Decide whether cgroup-v2 `memory.high` (if set) should pull RED earlier than `memory.max`.
- Evaluate subscribing the governor to the existing `system_metrics_collector` sampler instead of spawning a second psutil poller.
- Confirm `FilesystemVectorStore` construction site for attaching the `memory_governor` reference; confirm FTS cache exposes `invalidate`/`evict`/`get_stats` before injecting.
