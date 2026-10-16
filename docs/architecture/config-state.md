# Server Configuration State

Maintainer reference for how the server stores, changes and propagates its configuration: the split between
bootstrap keys in `config.json` and runtime settings in the database, the compare-and-set writers of the runtime
row, committed reads, change notification, and the launch-parameter files used to restart nodes. Operators change
runtime settings through the Web UI config screen.

All paths below are relative to `src/code_indexer/server/`.

## Contents

- [Two tiers](#two-tiers)
- [The runtime row](#the-runtime-row)
- [Startup](#startup)
- [Writing a change](#writing-a-change)
- [Reading](#reading)
- [Change notification](#change-notification)
- [Launch parameters and restarts](#launch-parameters-and-restarts)

## Two tiers

| Tier | Where | Contents |
|------|-------|----------|
| Bootstrap | `config.json` in the server directory (`CIDX_SERVER_DATA_DIR`, default `~/.cidx-server`) | Only the keys in `BOOTSTRAP_KEYS` (`services/config_service.py`): settings needed before the database is reachable |
| Runtime | The `server_config` table, row `config_key = 'runtime'`: SQLite `cidx_server.db` in solo mode, PostgreSQL in a cluster | Every other `ServerConfig` field |

`BOOTSTRAP_KEYS` is the authority for the split. It contains `server_dir`, `storage_mode`, `postgres_dsn`, `ontap`,
`cluster`, `clone_backend`, `cow_daemon`, `pace_maker_clone_path`, `fault_injection_enabled`,
`fault_injection_nonprod_ack`, `enable_malloc_arena_max`, `enable_malloc_trim`, `enable_graph_channel_repair`, the
four `graph_repair_*` flags, `server_threadpool_size`, `mcp_dispatch_pool_size`, `query_executor_pool_size`,
`enable_predeactivation_leak_scan` and `orphan_trash_sweep_per_startup_cap`. `host`, `port`, `workers` and
`log_level` are runtime settings.

Server code is expected to read settings with `get_config_service().get_config()`, not by loading `config.json`
directly and not from environment variables. (Known exception: the HNSW and FTS cache singletons are first built from
`CIDX_INDEX_CACHE_*` / `CIDX_FTS_CACHE_*` environment variables or defaults, `cache/__init__.py`; the runtime
`cache_config` values reach them only through the change paths described below.)

## The runtime row

The row holds the runtime settings as JSON plus a `version` integer, `updated_at` and `updated_by`. Every write of
the row goes through `services/config_runtime_row.py`; no other module contains SQL that writes it. Two rules hold for
every write:

- **Compare-and-set.** A commit names the version it was computed from and writes only while the row is still at
  that version. PostgreSQL: one `UPDATE ... WHERE config_key = 'runtime' AND version = %s RETURNING version`.
  SQLite: inside one `BEGIN IMMEDIATE`, re-read the row and update only when the version matches. Either returns the
  new version, or `None` when another process committed first.
- **Insert-only seeding.** First boot inserts the row with `ON CONFLICT DO NOTHING`; a row another process seeded is
  never overwritten.

`launch_restart_generation` is stored in the row but is not a `ServerConfig` field: a settings commit carries the
stored value over unchanged, and only `bump_generation_*` changes it.

| Writer | `updated_by` | Path |
|--------|--------------|------|
| First-boot seed | `config-seed` | `seed_runtime_*` |
| Settings changed by a person (Web UI, admin APIs) | `web-ui` | `update_settings_audited`, `apply_audited_change` |
| Settings changed by a system component | `system` | `apply_system_change` |
| Startup migrations of the stored row | `startup-migration` | `_rewrite_committed_row` |
| Whole-config save | `web-ui` | `save_config` |
| Cluster restart request | `launch-restart` | `bump_launch_restart_generation` |

`tools/migrate_to_postgres.py` is an offline operator tool that writes the table unconditionally; it must not run
against a live cluster.

## Startup

- **Solo** (`startup/service_init.py` calls `ConfigService.initialize_runtime_db()` with
  `<server_data_dir>/data/cidx_server.db`):
  when a runtime row exists it is merged over the bootstrap file; otherwise the current configuration is seeded,
  and the process adopts the committed row from one read (`_adopt_committed_row`), since a peer worker may have
  seeded or saved first.
- **Cluster** (`ConfigService.set_connection_pool()` in `startup/lifespan.py`): the row is loaded from PostgreSQL,
  seeding and adopting it in the same way when absent.
- After the runtime row is in place, `config.json` is stripped to the bootstrap keys
  (`_strip_config_file_to_bootstrap`); the original is copied once to
  `config-migration-backup/config.json.pre-centralization` in the server directory.

## Writing a change

All setting changes go through `ConfigService._change_config(mutate, before_publish, attempt)`:

1. Read the committed row and its version, and compose it over this process's bootstrap keys (the pre-image). The
   process's cached configuration is never used as the base, because another worker or node may have committed
   since it was loaded.
2. Deep-copy the pre-image into a candidate and apply `mutate`.
3. Validate the candidate (`config_manager.validate_config`) and run `before_publish`.
4. Commit with compare-and-set on the version read in step 1. If another process committed in between, start again
   from step 1; after `_CHANGE_ATTEMPTS` (10) lost attempts, raise `ConfigChangeConflict` and publish nothing.
5. On success, the candidate becomes this process's cached configuration, `launch.json` is rewritten, and the
   bootstrap keys are written to `config.json`. A failure of that file write after the commit raises
   `BootstrapFileNotWritten`; the change is still published.

A process-local re-entrant lock serializes changes within a process; no database lock is held across `mutate`,
validation or `before_publish`. Anything that raises before the commit publishes nothing.

Entry points:

- `update_settings_audited(updates, actor)` / `apply_audited_change(mutate, actor, ...)`: human changes; each records
  one audit row with the changed key names (`services/config_change_audit.py`), `success` only when published.
- `update_settings_atomic(updates)`: the same publish path without an audit row.
- `apply_system_change(mutate)`: changes made by server components. Code that needs to change a setting uses this
  with a `mutate` function; it never writes `get_config()`'s cached object back.
- `save_config(config)`: commits a whole configuration with compare-and-set against the version this process last
  loaded or committed. If another process committed since, it raises `ConfigChangeConflict` instead of overwriting.

Without a runtime database (early bootstrap only), changes are written to `config.json`.

## Reading

| Read | Freshness |
|------|-----------|
| `get_config()` | This process's cached configuration. Updated by this process's own commits and, in a cluster, by the 30 s reload. In solo mode a worker never reloads another worker's commit. |
| `read_committed_section(section)` | One read of the committed row, returning `(version, section dict)`. Sees a save made by any worker or node; does not update the cache. |

Code that must act on another process's save without waiting for a restart, such as the SIEM delivery loop, reads
the committed row with `read_committed_section()` each cycle.

## Change notification

- `register_on_commit_callback(callback)` runs `callback(before, after)` in the process that committed, after the
  commit and outside the change lock; it returns an unregister handle. A failing callback is logged; the change
  stays committed. The SIEM delivery scheduler uses it (`services/siem_delivery/scheduler.py`).
- `register_on_change_callback(callback)` runs when the cluster reload loads a new version from PostgreSQL. Startup
  registers callbacks that rewrite `launch.json`, re-apply cache settings to the HNSW and FTS cache singletons
  (`reapply_live_cache_hot_reload_fields`), and refresh components holding configuration by reference
  (`startup/lifespan.py`).
- **Cluster reload.** `start_config_reload(interval_seconds=30)` starts a thread only when a PostgreSQL pool is set.
  Each tick compares the row version with the last version this process saw (`check_config_update`), reloads and
  fires the change callbacks when it differs, then runs `check_pending_launch_restart()`. Database errors back off
  through `DbOutageThrottle`.

## Launch parameters and restarts

`host`, `port`, `workers` and `log_level` take effect only when uvicorn is restarted. The files involved live in
`$CIDX_DATA_DIR` (default `~/.cidx-server`; paths defined in `auto_update/deployment_executor.py`):

| File | Written by | Content |
|------|------------|---------|
| `launch.json` | `ConfigService.materialize_launch_config()` at startup, after every commit and on cluster reload | Target `workers`, `log_level`, `host`, `port`, `target_restart_generation` |
| `applied_launch.json` | The auto-updater, after restarting the server with `launch.json` | Applied values and `applied_restart_generation` |
| `restart.signal` | `check_pending_launch_restart()` | Request for the auto-updater to restart this node |

A cluster restart requested from the Web UI calls `bump_launch_restart_generation()`, a compare-and-set increment of
`launch_restart_generation`. It does not advance the version this process has recorded, so the bumping node's own
next poll sees the change as well. On every poll each node compares the row's generation with
`applied_restart_generation`; when the target is higher it rewrites `launch.json` and, only if that succeeded, writes
`restart.signal`. After more than 10 consecutive pending polls it logs one WARNING. In solo mode a restart request
does not bump the generation; it uses the single-node restart path.
