# Dependency Map

Maintainer reference for the server's dependency map: the per-domain Markdown files Claude writes under
`cidx-meta/dependency-map/`, the parser behind the `depmap_*` MCP tools, the Phase 3.7 graph repair, resumable
delta analysis, the coordination that keeps one analysis running at a time, and the optional git backup of
`cidx-meta`.

All paths below are relative to `src/code_indexer/server/` unless they start with `src/`.

## Contents

- [On-disk layout](#on-disk-layout)
- [Parser and anomaly channels](#parser-and-anomaly-channels)
- [Coordination: sentinel and write lock](#coordination-sentinel-and-write-lock)
- [Resumable delta analysis](#resumable-delta-analysis)
- [Repair and Phase 3.7 graph-channel repair](#repair-and-phase-37-graph-channel-repair)
- [cidx-meta backup mirror](#cidx-meta-backup-mirror)

## On-disk layout

The dependency map lives in the mutable `cidx-meta` golden repository:

| Path | Content |
|------|---------|
| `<golden_repos_dir>/cidx-meta/dependency-map/<domain>.md` | One file per domain: YAML frontmatter plus the Markdown body Claude produced (tables of incoming and outgoing dependencies) |
| `<golden_repos_dir>/cidx-meta/dependency-map/_domains.json` | Domain list with participating repositories |
| `<golden_repos_dir>/cidx-meta/dependency-map/_index.md` | Index regenerated from the domain files |
| `<golden_repos_dir>/cidx-meta/dependency-map.staging/` | Working directory of a full analysis before it is swapped in |
| `<golden_repos_dir>/cidx-meta/dependency-map/_active_<op_type>.lock` | Re-entrancy sentinels (see below) |

`DependencyMapService.get_sentinel_dir()` (`services/dependency_map_service.py`) is the single source of the
sentinel directory. Analyses run as background jobs with operation types `dependency_map_full`,
`dependency_map_delta` and `dependency_map_repair`. They are started by the MCP tool `trigger_dependency_analysis`
(`mode` is `full` or `delta`), by the Web UI dependency-map routes (`web/dependency_map_routes.py`), and by the
service's own scheduled delta, which is followed by an automatic repair attempt.

## Parser and anomaly channels

The read side is `DepMapMCPParser` (`services/dep_map_mcp_parser.py`), split into four modules:

| Module | Responsibility |
|--------|----------------|
| `services/dep_map_mcp_parser.py` | Orchestration and the public API |
| `services/dep_map_parser_tables.py` | Markdown table extraction |
| `services/dep_map_parser_hygiene.py` | Identifier normalization, the anomaly types and dataclasses, dedup and aggregation |
| `services/dep_map_parser_graph.py` | Graph edge aggregation, bidirectional consistency, channel split |

`DepMapMCPParser(dep_map_path)` exposes `find_consumers`, `get_repo_domains`, `get_domain_summary`,
`get_stale_domains` and two graph methods:

- `get_cross_domain_graph()` returns `(edges, anomalies)`, with each anomaly as a plain `{file, error}` dict.
- `get_cross_domain_graph_with_channels()` returns `(edges, all, parser_anomalies, data_anomalies)` with typed
  `AnomalyEntry` / `AnomalyAggregate` objects. Phase 3.7 repair consumes this form.

**Anomaly types.** `AnomalyType` (`services/dep_map_parser_hygiene.py`) binds each variant to its channel, so
routing is a lookup on `AnomalyType.channel`:

| Channel | Variants |
|---------|----------|
| `parser` | `MALFORMED_YAML`, `PATH_TRAVERSAL_REJECTED` |
| `data` | `BIDIRECTIONAL_MISMATCH`, `SELF_LOOP`, `GARBAGE_DOMAIN_REJECTED`, `CASE_NORMALIZATION_APPLIED` |

`aggregate_anomalies()` collapses a type whose total count exceeds 5 into one `AnomalyAggregate` carrying the first
examples; it serializes as `{"file": "<aggregated>", "error": "<N> occurrences: <type>"}`.

**Graph hygiene rules** (`services/dep_map_parser_graph.py`, `services/dep_map_parser_hygiene.py`):

- `strip_backticks()` removes every leading and trailing backtick; domain names are normalized (backticks stripped,
  lowercased) before comparison.
- Bidirectional consistency is checked per unordered pair, keyed by `frozenset({source, target})`, so one mismatch
  is reported per pair rather than per direction.
- `finalize_graph_edges()` drops edges with no derivable dependency types, except self-loops, which are always
  kept as edges and also reported. Anomalies it emits run through the same aggregation and channel split.

**MCP responses** (`mcp/handlers/depmap.py`). `depmap_get_cross_domain_graph` returns `anomalies`,
`parser_anomalies` and `data_anomalies`. `depmap_find_consumers`, `depmap_get_repo_domains`,
`depmap_get_domain_summary` and `depmap_get_stale_domains` return only `anomalies`. `depmap_get_hub_domains`
returns no anomaly field. The cross-domain-graph handler serializes the typed anomalies with `_anomaly_to_dict()`
(handling both `AnomalyEntry` and `AnomalyAggregate`); the other four tools pass through the `{file, error}` dict
lists the parser already serialized.

## Coordination: sentinel and write lock

Two independent mechanisms keep analyses from overlapping.

**Re-entrancy sentinel** (`services/shared_job_sentinel.py`). `SharedJobSentinel.try_claim(op_type, job_id,
node_id)` creates `_active_<op_type>.lock` with `os.open(O_CREAT | O_EXCL)` in the shared sentinel directory, so
only one node can hold a given op type. A sentinel older than its stale timeout is replaced:

| op_type | Stale timeout | Defined in |
|---------|---------------|------------|
| `analysis` | 14400 s (4 h) | `ANALYSIS_STALE_TIMEOUT_SECONDS`, `services/dependency_map_service.py` |
| `dashboard` | 1800 s (30 min) | `DASHBOARD_STALE_TIMEOUT_SECONDS`, `web/dependency_map_routes.py` |

`release()` deletes the sentinel only when its job id matches. Front doors claim synchronously before starting the
background thread and pass `pre_claimed=True` to `run_full_analysis()` / `run_delta_analysis()`; a failed claim
returns "already in progress" with the active job id. Job-row dedup across nodes uses
`JobTracker.register_job_if_no_conflict()` (see [jobs-and-cluster-state.md](jobs-and-cluster-state.md)).

**`cidx-meta` write lock.** `run_full_analysis()` and `run_delta_analysis()` first call
`RefreshScheduler.acquire_write_lock("cidx-meta", owner_name="dependency_map_service")`
(`src/code_indexer/global_repos/refresh_scheduler.py`). The acquire is non-blocking: when another writer, for
example a refresh publish, holds the lock, the analysis is skipped and the job completes as a skip. The lock is
provided by `AliasLockCoordinator` (`src/code_indexer/global_repos/alias_lock_coordinator.py`): a lock file under
`<golden_repos_dir>/.locks/` by default, or a database-backed store when that rollout flag is on. Long phases call
`raise_if_write_lock_ownership_lost()`, which renews the lock and aborts the run if ownership was lost. The
per-domain journal below depends on this single-writer guarantee; it does not replace it.

## Resumable delta analysis

A delta analysis calls Claude once per affected domain plus once for new-repository discovery. To avoid repeating
completed domains after a crash or restart, the domain file itself is the journal: its frontmatter records the
last delta applied. There is no separate cursor file.

Primitives, all in `services/dep_map_delta_journal.py`:

| Function | Contract |
|----------|----------|
| `compute_delta_fingerprint(changed, new, removed)` | SHA-256 of canonical JSON of the sorted alias lists; order-independent; a different repository set gives a different fingerprint |
| `parse_frontmatter(md_text)` | Splits `(dict, body)`; returns `({}, original_text)` with a WARNING on malformed YAML or a non-mapping block |
| `render_md(frontmatter, body)` | `---` block plus body; key order preserved |
| `validate_rendered_frontmatter(rendered, expected)` | Strict check before writing: the block must parse and `participating_repos` must match |
| `write_atomic(path, content)` | Temp file in the same directory, `fsync`, `os.replace`; the temp file is removed on failure |
| `all_new_repos_have_domain_assignments(new_repos, domains_json)` | True only when every new alias is a member of a domain in a readable, well-formed `_domains.json` |

The per-domain loop (`DependencyMapService._update_affected_domains` with a `fingerprint`):

1. A domain whose frontmatter `last_delta_applied` equals the current fingerprint is skipped; the activity journal
   records `Resume: skipping <domain> (already applied)`.
2. A file that starts with `---` but whose frontmatter does not parse is refused: no Claude call, no write, an
   error is recorded. Phase 3.7's `MALFORMED_YAML` repair is the path that fixes it.
3. Otherwise Claude is invoked with the existing body. Frontmatter echoed back by Claude is stripped. An empty or
   whitespace-only response leaves the file unchanged and the journal unadvanced.
4. The new frontmatter keeps every existing key, sets `domain`, `last_delta_applied` and `last_applied_at`, is
   validated, and is written together with the body in one `write_atomic()`.

New-repository discovery is skipped when `all_new_repos_have_domain_assignments()` is true. A changed repository set
produces a new fingerprint, so every domain is processed again; there is no partial credit.

**Durability scope.** The co-write of frontmatter and body survives process crashes, `SIGKILL` and service
restarts. The directory is not `fsync`ed after the rename. A write that fails leaves the previous file in place,
and a run interrupted mid-domain re-processes at most that domain. Cluster shared storage is described in
[cluster.md](cluster.md).

## Repair and Phase 3.7 graph-channel repair

`DepMapRepairExecutor.execute()` (`services/dep_map_repair_executor.py`) repairs problems found by
`DepMapHealthDetector` in phases: 0 discover uncovered repositories, 1 re-analyze broken domains with Claude,
1.5 remove stale repository references, 2 remove orphan files, 3 reconcile `_domains.json`, 3.5 backfill JSON
metadata from Markdown, **3.7 repair graph-channel anomalies**, 4 regenerate `_index.md`, 5 re-validate.

Phase 3.7 (`_run_phase37`) reads the anomalies from `get_cross_domain_graph_with_channels()` and dispatches them by
type:

| Anomaly | Handler | Behaviour |
|---------|---------|-----------|
| `SELF_LOOP` | `run_phase37`, `services/dep_map_repair_phase37.py` | Deterministic deletion of the self-referencing row |
| `MALFORMED_YAML` | `run_malformed_yaml_repairs`, `services/dep_map_repair_malformed_yaml.py` | Re-emits the frontmatter from `_domains.json` and splices it onto the original body bytes; when the frontmatter bounds cannot be located, falls back to Phase 1 re-analysis |
| `GARBAGE_DOMAIN_REJECTED` | `services/dep_map_repair_garbage_domain.py` | Remaps the reference to a real domain, or records it for manual review when ambiguous |
| `BIDIRECTIONAL_MISMATCH` | `audit_one_bidirectional_mismatch`, `services/dep_map_repair_bidirectional.py` | Asks an LLM to confirm or refute the missing direction with citations, then verifies the citations (`services/dep_map_repair_bidirectional_verify.py`) before back-filling; runs only when the executor has an LLM invoker |

The BIDIRECTIONAL_MISMATCH prompt is `mcp/prompts/bidirectional_mismatch_audit.md`. The LLM call goes through
`build_dep_map_dispatcher()` (`services/dep_map_dispatcher_factory.py`), which routes to Claude, or to Codex by the
configured weight when Codex integration is enabled.

**Flags.** All are bootstrap keys in `config.json` (`BOOTSTRAP_KEYS`, `services/config_service.py`):

| Key | Values | Default |
|-----|--------|---------|
| `enable_graph_channel_repair` | `true` / `false` | `true`; `false` makes Phase 3.7 a no-op |
| `graph_repair_self_loop`, `graph_repair_malformed_yaml`, `graph_repair_garbage_domain`, `graph_repair_bidirectional_mismatch` | `disabled`, `dry_run`, `enabled` | unset, which the executor treats as `dry_run` |

With the defaults, Phase 3.7 detects and journals but writes nothing. `disabled` skips the type (recorded as
`type_disabled_by_config`); `dry_run` runs the handler without file writes.

**Dry-run report.** `trigger_dependency_analysis` with `dry_run_graph_only=true` calls
`DependencyMapService.run_graph_repair_dry_run()`, which runs Phase 3.7 synchronously with invocation-level dry run
(no writes, no journal) and returns per-type, per-verdict and per-action counts plus the writes that would happen.

**Journal.** Every repair decision appends one line to `dep_map_repair_journal.jsonl` in `$CIDX_DATA_DIR`
(default `~/.cidx-server`), written under a process-local lock (`RepairJournal`,
`services/dep_map_repair_phase37.py`). Each line has 12 fields: `timestamp`, `anomaly_type`, `source_domain`,
`target_domain`, `source_repos`, `target_repos`, `verdict`, `action`, `citations`, `file_writes`,
`claude_response_raw`, `effective_mode`. `verdict` is `CONFIRMED`, `REFUTED`, `INCONCLUSIVE` or `N_A`. `action` is
one of the `Action` enum values in the same module, for example `self_loop_deleted`, `malformed_yaml_reemitted`,
`garbage_domain_remapped`, `auto_backfilled`, `claude_refuted_pending_operator_approval` or
`pleaser_effect_caught`.

## cidx-meta backup mirror

The server can mirror the mutable `cidx-meta` directory to a git remote. The remote is a passive backup: local
content is always authoritative and nothing is ever merged from the remote.

**Configuration.** Runtime setting `cidx_meta_backup` (`enabled`, `remote_url`; `CidxMetaBackupConfig` in
`utils/config_manager.py`), saved through the Web UI config screen. Saving with a non-HTTP(S), non-`file://` URL
requires a managed SSH key for the URL's host. Saving with a remote URL runs `CidxMetaBackupBootstrap.bootstrap()`
immediately.

**Path.** All git operations run in `<server_data_dir>/data/golden-repos/cidx-meta/`
(`get_cidx_meta_path()`, `services/cidx_meta_backup/paths.py`), never inside a `.versioned/` snapshot.

**Bootstrap** (`services/cidx_meta_backup/bootstrap.py`), idempotent:

- No `.git`: `git init`, check out the remote's default branch (`detect_default_branch()` via
  `git remote show origin`, else `master`), write `.gitignore`, commit, add `origin`, push.
- Existing repository: converge `.gitignore` to contain `.code-indexer/` and `.snapshot-reader-leases/`; when
  `origin` differs from the configured URL, re-point it and push. A rejected push raises; bootstrap never
  force-pushes.

**Sync** (`CidxMetaBackupSync.sync()`, `services/cidx_meta_backup/sync.py`), during every refresh of
`cidx-meta-global` while backup is enabled (`src/code_indexer/global_repos/refresh_scheduler.py`):

1. `MetaDirectoryUpdater` writes description files, then bootstrap runs (cheap when nothing changed).
2. Local changes are committed as `auto: cidx-meta refresh @ <timestamp>`.
3. `git fetch origin` refreshes the lease. A fetch failure is recorded as a sync failure.
4. If nothing was committed and local `HEAD` equals `origin/<branch>`, the sync reports "skipped" and the refresh
   ends with "No changes detected", unless the refresh was started with `force_reset` or `regate`, in which case
   it continues to indexing.
5. Otherwise local `HEAD` is published with `git push --force-with-lease origin <branch>`. A lease mismatch or
   other push failure is recorded as a sync failure and heals on the next cycle; a diverged remote is overwritten.

Indexing runs after the sync whether or not it failed. A recorded sync failure makes the refresh job fail after
indexing completes ("refresh complete, indexing succeeded, but backup ..."). Every backup git call is bound to the
refresh job's cancel check.
