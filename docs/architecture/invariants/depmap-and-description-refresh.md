# Dependency Map, cidx-meta and Description-Refresh Invariants

Rules for the dependency-map pipeline, the cidx-meta backup and the repository description refresh. How the
dependency map works: [Dependency Map](../dependency-map.md). Index of all invariant groups: [README](README.md).

## Dep-map and cidx-meta

### Resumable delta analysis

`run_delta_analysis` resumes after a crash through a per-domain journal: each `dependency-map/<domain>.md` carries a
`last_delta_applied` frontmatter field, written together with the body in one atomic `os.replace`. There is no
separate cursor file. Cluster correctness comes from the cidx-meta write lock. Durability covers process crash,
SIGKILL and restart, not sudden power loss or an NFS server crash. Detail: [Dependency Map](../dependency-map.md).

### Parser modules and anomaly channels

The parser is split into four modules (mcp parser, tables, hygiene, graph). Anomalies classify themselves through
`AnomalyType.channel`. Two APIs coexist: `get_cross_domain_graph()` (2-tuple) and
`get_cross_domain_graph_with_channels()` (4-tuple). Self-loops are always preserved.

### Re-entrancy sentinels

- Dep-map coordination state lives on the shared cidx-meta tree, never in per-node SQLite. `SharedJobSentinel`
  (`src/code_indexer/server/services/shared_job_sentinel.py`) claims `cidx-meta/dependency-map/_active_{op_type}.lock`
  with an atomic `O_CREAT|O_EXCL` create; only the owner releases it; stale sentinels are recovered.
- Two independent operation families: `analysis` (stale after `ANALYSIS_STALE_TIMEOUT_SECONDS = 14400`) and
  `dashboard` (stale after `DASHBOARD_STALE_TIMEOUT_SECONDS = 1800`).
- The sentinel directory comes only from `DependencyMapService.get_sentinel_dir()`, and it is the mutable cidx-meta
  path, never a `.versioned/` snapshot.
- Claim order in both front doors (Web `trigger_dependency_map`, MCP `trigger_dependency_analysis`):
  `is_available()` (409 with the active job id), then `try_claim()` in the request handler, then
  `register_job_if_no_conflict` (on `DuplicateJobError`, release the sentinel and return 409), then start the worker
  with `pre_claimed=True` so it does not claim again.
- The dashboard job registers with the non-NULL `repo_alias` `__depmap_dashboard__` so the database unique index also
  covers it; its cache is written to `_dashboard_cache.json` by temporary file and `os.replace`.

### Graph-channel repair (phase 3.7)

SELF_LOOP, MALFORMED_YAML and GARBAGE_DOMAIN_REJECTED are repaired deterministically; BIDIRECTIONAL_MISMATCH is
audited by Claude with the externalised prompt `bidirectional_mismatch_audit.md`. Bootstrap flag
`enable_graph_channel_repair` (default `True`). The append-only journal is `dep_map_repair_journal.jsonl` in the
server data directory (`CIDX_DATA_DIR`, default `~/.cidx-server`).

### cidx-meta backup

- The backup remote is a passive mirror of local cidx-meta. `CidxMetaBackupSync.sync()`
  (`src/code_indexer/server/services/cidx_meta_backup/sync.py`) commits local changes and pushes local `HEAD` with
  `git push --force-with-lease`. It never rebases and has no conflict resolver; a diverged remote is overwritten on
  the next cycle.
- Sync runs before indexing in the refresh path; a push failure is recorded and fails the job after indexing
  completes.
- All git operations run on the mutable base path (`get_cidx_meta_path()`), never inside `.versioned/`.
- Git authentication uses `build_non_interactive_git_env()` with no `-i`/`-F`, so the deploy key is resolved through
  the node's `~/.ssh/config`. At startup `SSHKeySyncService` writes the keys stored in the database to `~/.ssh/` and
  regenerates the managed `~/.ssh/config` section from each key's host assignments, pointing `IdentityFile` at the
  node's own copy. Nodes converge from the database, not from manual setup.

## Description refresh

- Quarantine: `PROMPT_FAILURE_QUARANTINE_THRESHOLD = 3` consecutive failures stop rescheduling a repository
  (`src/code_indexer/server/services/description_refresh_scheduler.py`). The failure count resets on success. A
  quarantined repository is retried only when its on-disk commit differs from the one recorded at failure time
  (`_read_current_fingerprint`), never because `last_known_commit` is NULL. The counters are per process; the
  database dedup below is the primary control.
- Cross-worker dedup: refresh jobs register with `register_job_if_no_conflict`; the partial unique index
  `idx_active_job_per_repo` is the cluster-atomic arbiter. `except DuplicateJobError` precedes the generic handler and
  skips the repository.
- The scheduler and `meta_description_hook` share one tracking backend instance, selected in
  `src/code_indexer/server/startup/lifespan.py` (`backend_registry.description_refresh_tracking` in cluster mode, the
  SQLite backend in solo). Before the loop starts, `_reconcile_stale_next_run_rows()` spreads overdue `next_run` values
  across the refresh interval so a cutover does not trigger a burst of Claude calls.
- The only description-producing path is the lifecycle pipeline: `LifecycleBatchRunner._process_one_repo` ->
  `LifecycleClaudeCliInvoker`. A refresh refines the existing description: the existing body is passed as data into
  the externalised `lifecycle_refresh_addendum.md`; with no existing body the prompt is identical to the create-mode
  `lifecycle_unified.md`. An existing body larger than 64 KB is truncated before it enters the prompt
  (`_MAX_DESCRIPTION_BYTES` in `lifecycle_claude_cli_invoker.py`). Each write stamps a fresh `last_analyzed`.
- Frontmatter on refresh is merged preserve-by-default (`_merge_lifecycle_dict`): an omitted or degraded value keeps
  the existing one, keys are never dropped.
- Descriptions are timeless: change-relative wording ("recent", "newly", "no longer") is banned in both prompts.
