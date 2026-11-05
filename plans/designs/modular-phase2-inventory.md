# Modular phase 2: X-Ray and SCIP imports of `code_indexer.server`

Status: inventory for Phase 2 of [modular-packaging-addons.md](modular-packaging-addons.md) (exit criterion: `xray`
and `scip` import no `server.*`). Measured on master at 7496e6211 with an AST walk of every `.py` file under
`src/code_indexer/xray` and `src/code_indexer/scip`, counting `import` and `from ... import` statements at any depth
(module level, `TYPE_CHECKING` blocks and function bodies). Transitive imports (for example `xray` importing
`global_repos.regex_search`, which imports the server's ripgrep backend) are out of scope here; they belong to the
`global_repos` count.

## Totals

| Package | Statements | Module level | Function level | Distinct server modules |
|---|---|---|---|---|
| `xray` | 16 (in 2 of 11 files) | 1 (`TYPE_CHECKING`) | 15 | 9 |
| `scip` | 7 (in 3 of 29 files) | 1 (`TYPE_CHECKING`) | 6 | 6 |

The plan's figures (xray 14, scip 7) were a name-matched estimate; xray has 16 statements because two telemetry
helpers each import the same pair of modules.

## By need

| Need | Server symbols imported | Sites (file:line) | Count |
|---|---|---|---|
| Content seam: is the seam delegated, the source for a repo, its manifest | `content_backend_wiring.is_content_seam_delegated`, `file_service.content_seam_source`, `file_service.iter_manifest_entries`, `file_service.ManifestDeadlineExceeded`, `file_service.ContentSeamSource` (type), `storage.shared.content_backend.NOT_FOUND_FETCH_ERRORS` | xray/search_engine.py:30,72,78,130,1322; xray/rust_backend.py:3275; scip/query/backends.py:27,60,69,551 | 10 |
| Ripgrep placement (where Phase 1 regex runs) | `ripgrep_backend_wiring.server_regex_target` | xray/search_engine.py:118 | 1 |
| X-Ray execution placement (where an evaluator runs) | `xray_backend_wiring.server_xray_execution_backend`, `xray_backend_wiring.xray_execution_backend_for` | xray/rust_backend.py:3032,3052 | 2 |
| Runtime config (`storage_mode`, `postgres_dsn`) | `config_service.get_config_service` | xray/search_engine.py:211 | 1 |
| Cluster store for the evaluator cache | `storage.postgres.connection_pool.ConnectionPool`, `storage.postgres.xray_cache_backend.XrayCachePostgresBackend` | xray/search_engine.py:227,230 | 2 |
| Telemetry: metrics | `telemetry.manager.peek_telemetry_manager`, `telemetry.metrics_instrumentation.get_application_metrics` | xray/rust_backend.py:657,660,703,706 | 4 |
| Telemetry: spans | `telemetry.spans.create_span` | scip/generator.py:400 | 1 |
| Snapshot path policy (pure) | `query_path_cache.is_immutable_versioned_snapshot` | scip/query/backends.py:279 | 1 |
| Served-index read scope carried to a worker thread | `served_index_cache.carry_served_index_reads` | scip/query/composites.py:803 | 1 |
| Startup wiring, job tracking | none directly: both are reached through the placement functions above or by the handlers in `server/` | | 0 |

## What replaces them

Location: `src/code_indexer/kernel/`. Reasons: it is outside `server/`; it stays in the `code_indexer` import package
that the kernel distribution (`cidx-core`) keeps; it imports no server code and no third-party package, so its two
interface modules can move into `cidx-sdk` verbatim. `code_indexer.utils` was the other candidate, but it mixes
implementation helpers with interfaces and would not give the SDK a clean file to lift.

| Module | Contents | Moves to `cidx-sdk`? |
|---|---|---|
| `kernel/capabilities.py` | What the platform offers a feature, as Protocols, each with an install slot and a no-host default: `ContentSeamHost`, `RegexTargetHost`, `XrayPlacementHost`, `XrayCacheStoreHost`, `RuntimeConfigHost`, `TelemetryHost`, `ReadScopeHost` | Yes: these become `FeatureContext` capabilities |
| `kernel/seam_types.py` | Seam Protocols and DTOs moved out of the server: `ContentBackend`, `ContentEntry`, `ContentPage`, `FetchedFile`, the fetch-error sets, `ContentSeamSource`, `ManifestDeadlineExceeded`; `RipgrepBackend`, `RipgrepRun`; the X-Ray execution seam (`XrayExecutionBackend`, requests, outcome) and `XrayCacheBackend` | Yes: seam Protocols and DTOs |
| `utils/snapshot_paths.py` | The canonical versioned-snapshot predicates (pure path rules) | No: kernel utility; SCIP should receive it as a capability once the SDK exists |

The server installs its implementations in `server/kernel_wiring.py`, called from `server/__init__.py` (so every
process that loads any server module, including the app, its workers and server tests, is wired before X-Ray or SCIP
runs) and from the `cidx xray` CLI (`cli_xray.py`), which read the server's runtime config for the cluster evaluator
cache before. `cidx scip` does not install them: everything it reached is either pure (moved) or a no-op outside a
server, and its import-budget test forbids loading the server package. Each adapter looks the server function up at
call time, so tests that patch the server binding keep working. Old import paths re-export the moved names.

## After

`xray` and `scip` import nothing from `code_indexer.server` (0 of 16 and 0 of 7 statements remain), enforced by
`tests/unit/kernel/test_xray_scip_import_boundary.py`, which also covers `code_indexer.kernel`.

A capability with no host installed answers what the server functions answered outside a running server: content
not delegated, local ripgrep, in-pod X-Ray execution, no telemetry, no read scope. The evaluator cache treats a missing
config host as "not yet known" and asks again on the next call, as it does today for a transient failure.

## Notes for later phases

- `XrayCachePostgresBackend` (in `server/storage/postgres`) and `xray_execution_backend_for` (in
  `server/startup/xray_backend_wiring.py`) are X-Ray code living in the server. Phase 4 moves them into `cidx-xray`;
  `XrayCacheStoreHost` then becomes a generic "PostgreSQL pool" capability and `XrayPlacementHost` disappears into the
  feature, using the content-seam capability.
- `kernel/seam_types.py` holds only Protocols and DTOs. The local implementations (`LocalContentBackend`,
  `LocalRipgrepBackend`, `LocalXrayExecutionBackend`) were not moved.
- The AST guard `tests/unit/kernel/test_xray_scip_import_boundary.py` asserts zero server imports in both packages;
  Phase 1's import-linter contract supersedes it when it lands.
