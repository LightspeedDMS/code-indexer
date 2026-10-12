# cidx xray: why the runtime image ships a Rust compiler

**Symptom** — `xray_search` / `xray_explore` return `matches: []` with
`files_processed == files_total`, no `partial` flag and no error: indistinguishable
from "scanned everything, found nothing". Or, after the first fix, every xray call
fails with `XRayCliError: Failed to invoke rustc ... Is rustc installed and on PATH?`

## Root cause

Three separate defects stacked:

1. **`xray-cli` was never built or shipped.** It is a Rust artifact; `pip install`
   cannot carry it, and `pyproject`'s `packages = ["src/code_indexer"]` would not
   include it even if it existed. The Python default path walks up from the
   installed package looking for `rust/` — which does not exist under
   `site-packages`. Every xray call failed to find a binary.
2. **The failure was silent.** `run_batch`'s missing-binary branch emitted one
   error tuple *per file*, each carrying that file's real path.
   `XRaySearchEngine.run()` discriminates a batch-level failure by
   `file_path == ""`, so per-file paths made it count every unscanned file as
   processed. The most dangerous shape an API can return: the caller's natural
   reading ("the pattern has no hits") is the exact opposite of the truth.
3. **The binary alone is not enough.** `xray-cli` compiles each evaluator at
   **query** time by spawning `rustc`
   (`rust/xray-core/src/compiler/rustc_driver.rs`).

## Why evaluators cannot be prebuilt

An xray query **is Rust source**. The user supplies `fn evaluate_node(...)`
(Legacy) or `collect_facts` + `analyze_graph` (Graph); both `EvaluatorMode`
variants compile to a `.so` and there is **no interpreted path**. The cache key is
a hash of the assembled source, so there is no fixed set to build ahead of time.

The compile cache cannot cover for it either:
- `cache::probe_rustc_version()` degrades to the literal string `"unknown"` when
  rustc is absent, so a rustc-less pod computes a *different* cache identity and
  misses every entry a seeder wrote.
- `LOCAL_CACHE_TTL_SECS = 300` expires a seeded local cache in five minutes.

## Fix

Ship the toolchain, copied **out of the `xray-builder` stage** — not installed
fresh. `xray-core/build.rs` bakes the build compiler's full `rustc --version`
into the binary as `CIDX_HOST_RUSTC_VERSION`, and `dynlib.rs` compares it
**byte-for-byte** against each evaluator `.so`, refusing to dlopen on any
difference. A separately installed 1.98.0 differing only by build hash would
compile every evaluator and then fail every load, at query time, long after deploy.

```bash
# verify the shipped image end to end
docker build -t cidx:check .
docker run --rm cidx:check bash -c 'rustc --version; which cargo rustdoc || echo "cargo/rustdoc pruned"'
docker run --rm cidx:check "$CIDX_XRAY_CLI_PATH" --print-graph-extractor-extensions
```

**Do not** trust a version check that compares the runtime toolchain against a
sidecar file written by the same builder stage — that is a tautology and a green
build can still ship a binary that cannot exec. The check must read the string
out of the shipped binary.

## Measured costs

| | |
|---|---|
| compiled evaluator `.so` | **4.28 MiB** (floor 4.26 MiB, however trivial) |
| toolchain, pruned (no cargo/rustdoc/docs/std-src) | 509 MiB |
| gcc + libc6-dev | 180 MiB |
| image delta, `docker images` | **+972 MB** (2.70 GB → 3.67 GB) |
| image delta, file content (`docker export`) | +684 MiB |

The two image numbers differ because the containerd image store counts retained
compressed blobs alongside the unpacked snapshot. Quote the method with the number.

Irreducible: libLLVM 191 MiB (rustc's codegen backend), librustc_driver 144 MiB,
std rlibs 173 MiB, plus a C linker. The toolchain's bundled `rust-lld` is **not**
a substitute for gcc — it cannot resolve `-lgcc_s/-lc/-lm` without gcc's
spec-file search paths, and `evaluator_rustc_command` passes no linker flags.

Target nodes: tools-* Karpenter `general-purpose`, **50 GiB gp3 roots**, so the
image moves from ~5% to ~7% of node disk.

## The Postgres evaluator cache

`xray_evaluator_cache` stores `.so` blobs for cross-node sharing. The TTL is a
**capacity** bound, not a correctness one: `source_hash` is the composite identity
`sha256(assembled_source) + ABI + rustc_version` and `fetch()` re-filters
`rustc_version`, so age cannot make a row *wrong*. A toolchain bump changes the
key, leaving old rows unreachable rather than stale.

At the original 300s the TTL recompiled any evaluator not re-run within five
minutes, on every node, forever. Raised to 7 days — but that interacts:

- At 4.28 MiB/row (1.65 MiB stored after TOAST+pglz), a 10 GiB PVC fills at
  ~4,900 rows. A full Postgres **refuses writes**, so sessions, jobs, leases and
  leader election all fail, two hops from a cache that swallows its exceptions.
  The table is therefore capped by **row count** (`XRAY_CACHE_MAX_ROWS`), not age
  alone.
- `pg_dump` emits `bytea` as hex (2 chars/byte), so the registry backup CronJob
  carried ~1.34 MiB per evaluator per dump × 30 retained. It now passes
  `--exclude-table-data=xray_evaluator_cache` — a cache has no place in a
  durability backup.
- Deletes are **chunked**: a matching DELETE measured 2.0 s per 500 rows (TOAST
  chunk deletion, not the seq scan — an index on `compiled_at` is NOT needed and
  measured 1.7 ms).

```sql
-- check growth on the cidx postgres
SELECT count(*), pg_size_pretty(pg_total_relation_size('xray_evaluator_cache'))
FROM xray_evaluator_cache;
```

## The environment xray-cli runs with

xray-cli loads caller-supplied code, so it never inherits the server's
environment. Every spawn passes `env=xray_subprocess_env()`
(`src/code_indexer/xray/rust_backend.py`), which keeps only `PATH`, `HOME`,
`TMPDIR`/`TMP`/`TEMP`, `LANG`/`LANGUAGE`/`LC_*`, `CIDX_DATA_DIR`, `RUSTUP_HOME`
and `CARGO_HOME`. A test fails any new xray-cli spawn without it. A rustc that
needs another variable has to be added to that allowlist, with a reason.

| Symptom | Cause | Fix |
|---|---|---|
| `Is rustc installed and on PATH?` only from the server | rustc's directory is missing from the server's own `PATH` | put it on the server's `PATH`; xray passes `PATH` through |
| rustup: `no default toolchain` / `toolchain not installed` | rustup lives outside `$HOME` and its vars were not set for the server | export `RUSTUP_HOME`/`CARGO_HOME` for the server |
| `CompileError` mentioning `memory` / `out of memory` | rustc hit the compile heap bound (1 GiB default) | raise `CIDX_XRAY_COMPILE_RLIMIT_DATA_BYTES` (bytes) on the server |

`CIDX_XRAY_COMPILE_RLIMIT_DATA_BYTES` bounds the heap of every compile: the
`--compile-only` spawn, and on the scan path the rustc child that xray-cli
starts. The scan itself is not capped.

## Gotcha: only the root node reaches `evaluate_node`

A legacy evaluator is called with the file's **root** node. Checking
`node.kind == "function_definition"` at the top level returns nothing; walk
children yourself. Also note `kind` and `start_line` are **fields, not methods** —
`node.kind()` is a compile error.

## Related

- [CIDX_GIT_SEAM_GUARD.md](git-seam-guard.md) — the CI guard and credential redaction
- [CIDX_UPSTREAM_MERGE_CORRECTIONS.md](upstream-merge-corrections.md) — claims from this work that proved false, and why
