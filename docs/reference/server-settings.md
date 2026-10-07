# Server Settings Reference

Reference for CIDX Server operators: every setting the administration Web UI's Configuration screen
(`/admin/config`) changes, with its default, the values it accepts and whether it needs a restart; the bootstrap keys
of `config.json`; and the settings that exist in the configuration model but have no control on that screen.

How the two tiers are stored, committed and propagated is described in
[Server Configuration State](../architecture/config-state.md). How to install and start a server is in
[Deployment](../server/deployment.md).

## Contents

- [How settings are stored and changed](#how-settings-are-stored-and-changed)
- [Reading the tables](#reading-the-tables)
- [Core Server](#core-server)
- [Search and Content](#search-and-content)
- [Repository Management](#repository-management)
- [Performance and Reliability](#performance-and-reliability)
- [Authentication and Security](#authentication-and-security)
- [AI and Integrations](#ai-and-integrations)
- [Observability](#observability)
- [Wiki](#wiki)
- [Settings changed outside the Configuration screen](#settings-changed-outside-the-configuration-screen)
- [Not editable in the Web UI](#not-editable-in-the-web-ui)
- [Bootstrap keys (config.json)](#bootstrap-keys-configjson)

## How settings are stored and changed

- **Bootstrap keys** live in `config.json` in the server data directory (`~/.cidx-server` unless
  `CIDX_SERVER_DATA_DIR` is set). The authoritative list is `BOOTSTRAP_KEYS` in
  `src/code_indexer/server/services/config_service.py`. See [Bootstrap keys](#bootstrap-keys-configjson).
- **Runtime settings** are every other field of `ServerConfig` (`src/code_indexer/server/utils/config_manager.py`).
  They are stored in the database (SQLite `cidx_server.db` standalone, PostgreSQL in a cluster) and changed on the
  Configuration screen. `host`, `port`, `workers` and `log_level` are runtime settings.

The Configuration screen is available to `admin` accounts. When step-up elevation enforcement is on, saving a
section requires an open elevation window ([Login and Elevation](../server/auth/login-and-elevation.md)).

Each section is saved through `POST /admin/config/{section}`, where `{section}` must be one of
`_VALID_CONFIG_SECTIONS` (`src/code_indexer/server/web/routes.py`). The route validates the form, then
`ConfigService.update_settings_audited()` (for the TOTP section, `update_totp_elevation_audited()`) applies all fields
of the section as one change: it is validated as a whole (`ServerConfigManager.validate_config()`), a rejected value
saves nothing, and one audit row records the change.

Secret fields (API keys, client secrets, passwords) are write-only: the screen never shows their value, and saving
the field empty keeps the stored secret.

Propagation: the process that served the save uses the new values at once. Cluster nodes reload the runtime
configuration every 30 seconds. On a standalone server with more than one worker process, the other workers do not
reload another worker's save; restart the server for every worker to use the new value
([Reading](../architecture/config-state.md#reading)).

**Reset to Defaults** (`POST /admin/config/reset`) replaces the whole configuration with
`ServerConfigManager.create_default_config()`, and the save then rewrites `config.json` from that configuration. This
replaces the bootstrap keys as well (for example `storage_mode` becomes `sqlite` and `postgres_dsn` empty), along
with every stored secret and `host`, `port` and `workers`.

## Reading the tables

- **Setting**: the form field name, which is also the field name in the configuration object named in each
  section heading.
- **Default**: the value a fresh installation uses (dataclass default in `config_manager.py`).
- **Allowed**: values the server accepts on save (route validation in `_validate_config_section()`, the per-field
  setters in `config_service.py`, or `validate_config()`). "Form only" marks a limit that only the HTML input
  enforces. "Raised to" marks a value the server silently raises to a minimum instead of rejecting.
- **Restart**: "yes" when the field is listed in `RESTART_REQUIRED_FIELDS` (`web/routes.py`): the value is read once
  at startup, and the Web UI shows "Requires server restart" next to it. An empty cell means the field is not on
  that list; this reference does not assert that every such field applies without a restart. Where the code reads a
  value on every use, the section notes say so.

## Core Server

### Server Settings

Section `server`, top-level `ServerConfig` fields.

| Setting | Default | Allowed | Restart | Notes |
|---------|---------|---------|---------|-------|
| `host` | `127.0.0.1` | IPv4/IPv6 address or hostname | yes | Bind address. A change requires confirming the dialog (`confirm_host_port_change`) |
| `port` | `8000` | 1 to 65535 | yes | A change requires confirming the dialog |
| `workers` | `1` | 1 to 64 | yes | uvicorn worker processes |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | yes | |
| `jwt_expiration_minutes` | `10` | 1 or more | yes | Lifetime of issued JWTs |
| `service_display_name` | `Neo` | text | | Name the server reports in MCP responses |

`host`, `port` and `workers` are flags of the systemd unit's `ExecStart`; saving them restarts nothing. They take
effect when a restart is requested from the Web UI and the auto-updater rewrites the unit
([Deployment](../server/deployment.md#bootstrap-configuration-configjson),
[Auto-Update](../server/auto-update.md#restart-requests-and-launch-settings)).

### Provider API Keys

Saved through `/api/api-keys/{provider}` (`src/code_indexer/server/routers/api_keys.py`), not the section form;
stored in `claude_integration_config`. The format of each key is checked on save, and each provider has a
connectivity test.

| Setting | Default | Notes |
|---------|---------|-------|
| `anthropic_api_key` | empty | Used by Claude CLI integrations |
| `voyageai_api_key` | empty | VoyageAI embeddings ([Embedding provider keys](../server/deployment.md#embedding-provider-keys)) |
| `cohere_api_key` | empty | Cohere embeddings |

### Subscription Mode (LLM Credentials Provider)

Saved through `POST /api/llm-creds/save-config` (`src/code_indexer/server/routers/llm_creds.py`); the generic section
save refuses `claude_auth_mode`, `llm_creds_provider_url` and `llm_creds_provider_api_key`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `claude_auth_mode` | `api_key` | `api_key`, `subscription` | `subscription` leases Anthropic credentials from the provider at runtime |
| `llm_creds_provider_url` | empty | URL; required for `subscription` | |
| `llm_creds_provider_api_key` | empty | required for `subscription` | Write-only |
| `llm_creds_provider_consumer_id` | `cidx-server` | text | Sent to the provider on checkout |

### MCP Session Settings

Section `mcp_session`, object `mcp_session_config`.

| Setting | Default | Allowed | Restart |
|---------|---------|---------|---------|
| `session_ttl_seconds` | `3600` | 60 or more | |
| `cleanup_interval_seconds` | `900` | 60 or more | |

### Background Task Workers

Section `background_jobs`, object `background_jobs_config`.

| Setting | Default | Allowed | Restart | Notes |
|---------|---------|---------|---------|-------|
| `max_concurrent_background_jobs` | `5` | 1 to 100 | yes | Jobs above the limit stay pending |
| `subprocess_max_workers` | `8` | 1 to 50 | yes | Pool for subprocess-based operations such as regex search |

### Data Retention

Section `data_retention`, object `data_retention_config`. Values in hours.

| Setting | Default | Allowed |
|---------|---------|---------|
| `operational_logs_retention_hours` | `168` | 1 to 8760 |
| `audit_logs_retention_hours` | `2160` | 1 to 8760 |
| `sync_jobs_retention_hours` | `720` | 1 to 8760 |
| `dep_map_history_retention_hours` | `2160` | 1 to 8760 |
| `background_jobs_retention_hours` | `720` | 1 to 8760 |
| `cleanup_interval_hours` | `1` | 1 to 24 |

### Activated Repository Reaper

Section `activated_reaper`, object `activated_reaper_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `ttl_days` | `30` | 1 to 3650 | Activated repositories not accessed within this window are deactivated |
| `cadence_hours` | `24` | 1 to 168 | How often the reaper runs |

### HNSW Orphan-Repair Sweep

Section `hnsw_orphan_sweep`, object `hnsw_orphan_repair_sweep_config`. The scheduler reads these values from the
configuration on each cycle (`services/hnsw_orphan_sweep/scheduler.py`). See
[Storage: HNSW integrity](../architecture/storage.md#hnsw-integrity).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `enabled` | `true` | `true`, `false` | |
| `operating_hours_start_utc` | `0` | 0 to 23 | Start equal to end means always on; start greater than end wraps overnight |
| `operating_hours_end_utc` | `0` | 0 to 23 | |
| `tick_interval_minutes` | `7` | 1 or more | |
| `batch_size` | `15` | 1 or more | Indexes checked per tick |

### SIEM Delivery

Section `siem_delivery`, object `siem_delivery_config`. Settings, credentials and arming are documented in
[SIEM Operations](../server/siem/operations.md#configuration).

### Indexing Watchdog

Section `indexing_watchdog`, object `indexing_watchdog_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `stale_activity_timeout_seconds` | `120.0` | 1.0 to 3600.0 | An indexing subprocess that makes no forward progress for this long is killed; it is not a job-duration limit |

### Fleet Migration

Section `fleet_migration`, object `fleet_migration_config`. See
[Storage: Moving a repository to CHUNKS_DB](../architecture/storage.md#moving-a-repository-to-chunks_db).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `enabled` | `false` | `true`, `false` | Consolidates sharded JSON indexes into `chunks.db` and deletes the shards after verification |
| `tick_interval_minutes` | `30` | 1 or more | Each tick migrates at most one repository |
| `canary_gate_enabled` | `false` | `true`, `false` | Holds the sweep after the first migrated repository until an admin confirms |

### Alias Lock

Section `alias_lock`, object `alias_lock_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `db_backed_enabled` | `true` | `true`, `false` | `true`: alias operations are serialized by a database-held lock; `false`: legacy JSON lock files |

### Temporal Legacy Migration

Section `temporal_legacy_migration`, object `temporal_legacy_migration_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `relocation_enabled` | `false` | `true`, `false` | Copies legacy in-repository temporal shards to the server-owned temporal location |
| `cleanup_authorized` | `false` | `true`, `false` | Allows deleting a legacy shard after its copy is verified |

### Query and Search Timeouts

Section `search_timeouts`, object `search_timeouts_config`. Values in seconds.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `search_code_handler_timeout_seconds` | `180` | 30 to 600 | Caps one `search_code` MCP call |
| `default_handler_timeout_seconds` | `60` | 10 to 300 | Caps synchronously dispatched MCP tools without their own timeout; not `regex_search` or other asynchronously dispatched tools |
| `write_mode_handler_timeout_seconds` | `720` | 600 to 3600 | Caps `exit_write_mode` |
| `embedding_provider_timeout_seconds` | `30` | 5 to 120 | Per outbound VoyageAI/Cohere embedding call |
| `reranker_timeout_seconds` | `15` | 5 to 120 | Per rerank call |
| `rest_query_handler_timeout_seconds` | `180` | 30 to 600 | Caps the temporal branch of `POST /api/query` |
| `temporal_inline_wait_seconds` | `60.0` | 0.0 to `search_code_handler_timeout_seconds` minus 1.0 | How long a temporal query waits inline before continuing as a background job |

### Embedding and Reranker Call Tracking

Section `embedding_stats`, object `embedding_stats_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `enabled` | `true` | `true`, `false` | Records embedding and rerank calls in `embedding_call_stats` |
| `flush_interval_seconds` | `30.0` | greater than 0 | |
| `retention_days` | `90` | greater than 0 | |

### Temporal Indexing

Section `temporal_indexing`, object `temporal_indexing_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `index_floor_date` | empty (full history) | `YYYY-MM-DD` or empty | Temporal indexing runs skip commits dated before it. Bounds future runs only. When a golden repository also has a floor date, the later date applies |

### X-Ray Search

Section `xray`, object `xray_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `xray_timeout_seconds` | `120` | 10 to 600 | Per-job wall-clock limit of an X-Ray search |
| `xray_worker_threads` | `4` | 1 to 8 | AST evaluation threads; the saving process also updates its X-Ray concurrency limit at once |

## Search and Content

### Search Limits

Section `search_limits`, object `search_limits_config`.

| Setting | Default | Allowed |
|---------|---------|---------|
| `max_result_size_mb` | `1` | 1 to 100 |
| `timeout_seconds` | `30` | 5 to 300 |

### Multi-Search Settings

Section `multi_search`, object `multi_search_limits_config`. The `omni_*` settings apply to MCP cross-repository
(omni) search.

| Setting | Default | Allowed | Restart |
|---------|---------|---------|---------|
| `multi_search_max_workers` | `8` | 1 to 50 | yes |
| `multi_search_timeout_seconds` | `30` | 5 to 600 | yes |
| `scip_multi_max_workers` | `8` | 1 to 50 | yes |
| `scip_multi_timeout_seconds` | `30` | 5 to 600 | yes |
| `omni_cache_max_entries` | `100` | 1 to 10000 | |
| `omni_cache_ttl_seconds` | `300` | 1 to 86400 | |
| `omni_default_limit` | `10` | 1 to 1000 | |
| `omni_max_limit` | `1000` | 1 to 10000 | |
| `omni_default_aggregation_mode` | `global` | `global`, `per_repo` | |
| `omni_max_results_per_repo` | `100` | 1 to 10000 | |
| `omni_pattern_metacharacters` | `*?[]^$+\|` | text | |
| `omni_wildcard_expansion_cap` | `50` | 1 to 10000 (form only) | |
| `omni_max_repos_per_search` | `50` | 1 to 10000 (form only) | |

`omni_wildcard_expansion_cap` limits how many repositories one wildcard pattern may expand to;
`omni_max_repos_per_search` limits the total after expansion and literal aliases are combined.

### Content Limits

Section `content_limits`, object `content_limits_config`. Token limits for content returned to AI clients.

| Setting | Default | Allowed |
|---------|---------|---------|
| `chars_per_token` | `4` | 1 to 10 |
| `file_content_max_tokens` | `50000` | 1000 to 200000 |
| `git_diff_max_tokens` | `50000` | 1000 to 200000 |
| `git_log_max_tokens` | `50000` | 1000 to 200000 |
| `search_result_max_tokens` | `50000` | 1000 to 200000 |
| `cache_ttl_seconds` | `3600` | 60 or more |

### Reranking

Section `rerank`, object `rerank_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `voyage_reranker_model` | empty | model name or empty | Empty disables Voyage reranking |
| `cohere_reranker_model` | empty | model name or empty | Empty disables Cohere reranking |
| `overfetch_multiplier` | `5` | 1 or more | Results fetched per requested result before reranking |

### Query Embedding Cache

Section `query_embedding_cache`, object `query_embedding_cache_config`. The cache reads these settings on every call;
the modes and the cache itself are described in
[Query Path: Query-embedding cache](../architecture/query-path.md#query-embedding-cache). The section also has a
button that clears the cache (`POST /admin/config/query-embedding-cache/clear`).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `query_embedding_cache_enabled` | `true` | `true`, `false` | `false` makes the cache inert |
| `query_embedding_cache_max_entries` | `10000` | 100 or more | Row cap shared by both providers |
| `query_embedding_cache_voyage_mode` | `shadow` | `off`, `shadow`, `on` | `shadow` caches for measurement but always serves the live vector; `on` serves cache hits |
| `query_embedding_cache_cohere_mode` | `shadow` | `off`, `shadow`, `on` | |
| `query_embedding_cache_voyage_anchor_tokens` | empty (2) | 0 or more, or empty | Leading query tokens kept in order when building the key |
| `query_embedding_cache_cohere_anchor_tokens` | empty (2) | 0 or more, or empty | |
| `query_embedding_cache_voyage_audit_sample_rate` | `0.0` | 0.0 to 1.0 | Fraction of cache hits audited against the live vector |
| `query_embedding_cache_cohere_audit_sample_rate` | `0.0` | 0.0 to 1.0 | |

## Repository Management

### Golden Repository Settings

Section `golden_repos`, object `golden_repos_config`.

| Setting | Default | Allowed | Restart | Notes |
|---------|---------|---------|---------|-------|
| `refresh_interval_seconds` | `3600` | 60 or more | | Interval of the periodic golden-repository refresh. Also settable with the MCP tool `set_global_config` |
| `analysis_model` | `opus` | `opus`, `sonnet` | yes | Claude model for description generation, dependency-map analysis and the Research Assistant. The restart applies to the scheduled analyses; the Research Assistant reads the value for each message |
| `externally_managed` | `false` | `true`, `false` | | `true`: an external owner creates and refreshes golden repositories; the server skips its periodic refresh and its startup restore reconciliation |

### Indexing Settings

Section `indexing`, object `indexing_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `voyage_ai_parallel_requests` | `8` | 1 to 32 | Concurrent VoyageAI embedding calls during indexing |
| `cohere_parallel_requests` | `8` | 1 to 32 | Concurrent Cohere embedding calls during indexing |
| `temporal_parallel_requests` | empty | 1 to 32, or empty | Threads for git-diff analysis in temporal indexing; empty inherits the provider setting |
| `indexable_extensions` | 60 extensions (below) | comma-separated list | Stored lowercase with a leading dot. Seeded into a golden repository's configuration at registration and synchronized at its next refresh when they differ |
| `temporal_embedders` | `voyage-context-4` | non-empty comma-separated list | Per-commit temporal embedders |
| `temporal_active_embedder` | `voyage-context-4` | one of `temporal_embedders` | |
| `temporal_aggregation_chunk_chars` | `4096` | 1 or more | Chunk size of the per-commit document before embedding |
| `temporal_all_branches_enabled` | `false` | `true`, `false` | When `false`, golden temporal indexing covers only the registered branch and requests for all branches are rejected |

Default `indexable_extensions`: `.py .js .jsx .ts .tsx .java .scala .kt .kts .groovy .c .h .cpp .cxx .cc .hpp .hxx
.cs .go .rs .rb .erb .php .swift .m .mm .r .lua .pl .pm .sh .bash .zsh .fish .ps1 .psm1 .bat .cmd .sql .html .htm
.css .scss .sass .less .xml .xsl .xsd .json .yaml .yml .toml .ini .cfg .conf .md .mdx .rst .txt .tex`.

### SCIP Code Intelligence

Section `scip`, object `scip_config`. The form has one field; the other SCIP limits are listed under
[Not editable in the Web UI](#not-editable-in-the-web-ui).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `temporal_stale_threshold_days` | `7` | 1 or more | Age after which a temporal index counts as stale |

## Performance and Reliability

### Cache Settings

Section `cache`, object `cache_config`. An empty TTL, cleanup-interval or payload field saves the default.

| Setting | Default | Allowed | Restart | Notes |
|---------|---------|---------|---------|-------|
| `index_cache_ttl_minutes` | `10.0` | greater than 0 | | HNSW index cache entry lifetime |
| `index_cache_cleanup_interval` | `60` | 1 or more | | Seconds between HNSW cache cleanups |
| `index_cache_max_size_mb` | empty | 1 or more, or empty | | Empty: 4096 MB per node, divided among the worker processes (at least 256 MB each) |
| `fts_cache_ttl_minutes` | `10.0` | greater than 0 | | Full-text index cache entry lifetime |
| `fts_cache_cleanup_interval` | `60` | 1 or more | | |
| `fts_cache_max_size_mb` | empty | 1 or more, or empty | | Same default as the HNSW cap |
| `payload_preview_size_chars` | `2000` | 1 or more (form: 100) | yes | Preview length of search results |
| `payload_max_fetch_size_chars` | `5000` | 1 or more (form: 100) | yes | Page size when fetching full content |
| `payload_cache_ttl_seconds` | `900` | 1 or more | yes | |
| `payload_cleanup_interval_seconds` | `60` | 1 or more | yes | |
| `memory_governor_enabled` | `true` | `true`, `false` | | `false` evicts index caches after each use |
| `memory_governor_yellow_pct` | `70.0` | greater than 0 and below the red threshold | | |
| `memory_governor_red_pct` | `85.0` | above the yellow threshold, at most 100 | | |
| `memory_governor_hysteresis_pct` | `10.0` | below the lower of yellow and 100 minus red | | Subtracted from the thresholds for leaving a band |
| `memory_governor_red_min_dwell_seconds` | `30` | integer (form: 0 or more) | | |
| `memory_governor_sample_interval_seconds` | `2.0` | number (form: 0.1 or more) | | |
| `memory_governor_swap_forces_red` | `true` | `true`, `false` | | Swap-in activity above the threshold forces the red band |
| `memory_governor_swap_pswpin_red_threshold` | `100` | 0 or more | | Swap-in pages per sample needed to force red |
| `memory_governor_rss_inflation_factor` | `2.0` | number (form: 1.0 or more) | | Stored and shown in the governor statistics; no runtime decision reads it |

The cache TTL, cleanup-interval and size-cap fields are pushed to the live HNSW and FTS caches when saved. The memory
governor reads its fields on each band decision.

### Timeouts and Indexing Limit Settings

Section `timeouts`, object `resource_config`. Values in seconds unless noted.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `git_clone_timeout` | `3600` | 1 or more | |
| `git_pull_timeout` | `3600` | 1 or more | |
| `git_refresh_timeout` | `3600` | 1 or more | |
| `hnsw_max_elements` | `1000000` | integer (form: 10000 or more) | Maximum vectors per HNSW index |
| `git_init_conflict_timeout` | `1800` | integer (form: 0 or more) | |
| `git_service_conflict_timeout` | `1800` | integer (form: 0 or more) | |
| `git_service_cleanup_timeout` | `300` | integer (form: 0 or more) | |
| `git_service_wait_timeout` | `180` | integer (form: 0 or more) | |
| `git_process_check_timeout` | `30` | integer (form: 0 or more) | |
| `git_untracked_file_timeout` | `60` | integer (form: 0 or more) | |
| `cow_clone_timeout` | `3600` | integer (form: 0 or more) | Copy-on-write clone during refresh |
| `cidx_fix_config_timeout` | `60` | integer (form: 0 or more) | |

### Lifecycle Analysis Timeouts

Section `lifecycle_analysis`, object `lifecycle_analysis_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `shell_timeout_seconds` | `1800` | 1 or more (form: 60) | Limit for one shell command in a lifecycle analysis |
| `outer_timeout_seconds` | `1860` | at least `shell_timeout_seconds` plus 30 (form: 90) | Limit for the whole analysis run |

### Health Check Thresholds

Section `health`, object `health_config`. Percentages. All are read once at startup.

| Setting | Default | Allowed | Restart |
|---------|---------|---------|---------|
| `memory_warning_threshold_percent` | `80.0` | 0 to 100 | yes |
| `memory_critical_threshold_percent` | `90.0` | 0 to 100 | yes |
| `disk_warning_threshold_percent` | `80.0` | 0 to 100 | yes |
| `disk_critical_threshold_percent` | `90.0` | 0 to 100 | yes |
| `cpu_sustained_threshold_percent` | `95.0` | 0 to 100 | yes |

How the health endpoints use them: [Observability](../server/observability.md#health-endpoints).

## Authentication and Security

### SSO Authentication

Section `oidc`, object `oidc_provider_config`. Setup and behaviour: [OIDC](../server/auth/oidc.md).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `enabled` | `false` | `true`, `false` | When `true`, `issuer_url` and `client_id` are required |
| `issuer_url` | empty | `http://` or `https://` URL | |
| `client_id` | empty | text | |
| `client_secret` | empty | text | Write-only |
| `scopes` | `openid profile email` | space-separated | Empty saves the default |
| `email_claim` | `email` | text; required with JIT provisioning | |
| `username_claim` | `preferred_username` | text; required with JIT provisioning | |
| `groups_claim` | `groups` | text | |
| `group_mappings` | empty list | JSON list (or object) | Maps identity-provider groups to CIDX groups |
| `use_pkce` | `true` | `true`, `false` | |
| `require_email_verification` | `true` | `true`, `false` | |
| `enable_jit_provisioning` | `true` | `true`, `false` | Creates an account on first SSO sign-in |
| `default_role` | `normal_user` | `normal_user`, `admin` (form) | Role of a JIT-provisioned account |

### Password Security

Section `password_security`, object `password_security`.

| Setting | Default | Allowed | Restart |
|---------|---------|---------|---------|
| `min_length` | `12` | 1 or more | yes |
| `max_length` | `128` | 1 or more | yes |
| `required_char_classes` | `4` | 1 to 4 | yes |
| `min_entropy_bits` | `50` | integer (form: 1 or more) | yes |

### Web Security

Section `web_security`, object `web_security_config`.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `self_registration_enabled` | `false` | `true`, `false` | When `false`, `POST /auth/register` answers 403. Read on every request |

### TOTP Step-Up Elevation

Section `totp_elevation`, top-level fields. Documented in
[Login and Elevation: Settings](../server/auth/login-and-elevation.md#settings).

| Setting | Default | Allowed |
|---------|---------|---------|
| `elevation_enforcement_enabled` | `false` | `true`, `false` |
| `elevation_idle_timeout_seconds` | `300` | 60 to 3600, not above the maximum age |
| `elevation_max_age_seconds` | `1800` | 300 to 7200, not below the idle timeout |

### Pace Maker

Section `pace_maker`, top-level field. See [Auto-Update: pace-maker](../server/auto-update.md#pace-maker).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `pace_maker_mode` | `disabled` | `disabled`, `on`, `off` | Applied before Claude CLI invocations: `disabled` leaves pace-maker untouched, `on` keeps it in pacing-only mode, `off` turns its master switch off |

### Operational Logging

Section `search_event_log`; both are top-level fields.

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `search_event_log_retention_days` | `90` | 1 to 3650 | Search events older than this are pruned once a day |
| `export_retention_days` | `30` | 1 to 3650 | Export files older than this are deleted |

## AI and Integrations

### Claude CLI Integration

Section `claude_cli`, object `claude_integration_config`. The Claude model these features use is
`analysis_model` ([Golden Repository Settings](#golden-repository-settings)).

| Setting | Default | Allowed | Restart | Notes |
|---------|---------|---------|---------|-------|
| `max_concurrent_claude_cli` | `2` | 1 or more (form: 1 to 10) | yes | Parallel Claude CLI processes for description generation |
| `description_refresh_enabled` | `false` | `true`, `false` | | Scheduled repository description refresh |
| `description_refresh_interval_hours` | `24` | 1 or more | | |
| `research_assistant_timeout_seconds` | `1200` | integer (form: 60 to 7200) | | Limit for one Research Assistant Claude CLI run ([Research Assistant](../server/research-assistant.md)) |
| `dependency_map_enabled` | `false` | `true`, `false` | yes | Dependency-map generation ([Dependency Map](../architecture/dependency-map.md)) |
| `dependency_map_interval_hours` | `168` | raised to 1 | | Delta analysis interval |
| `dependency_map_pass_timeout_seconds` | `1800` | raised to 60 | | Per-pass limit |
| `dependency_map_pass1_max_turns` | `0` | raised to 0 | | `0`: single-shot, no tools |
| `dependency_map_pass2_max_turns` | `0` | raised to 0 | | `0`: no turn cap |
| `dependency_map_delta_max_turns` | `0` | raised to 0 | | `0`: no turn cap |
| `refinement_enabled` | `false` | `true`, `false` | | Scheduled fact-checking of dependency documents |
| `refinement_interval_hours` | `24` | raised to 1 | | |
| `refinement_domains_per_run` | `3` | 1 to 50 (clamped) | | |
| `dep_map_fact_check_enabled` | `false` | `true`, `false` | | Verification pass after generation |
| `fact_check_timeout_seconds` | `600` | 60 to 3600 | | |
| `dep_map_auto_repair_enabled` | `false` | `true`, `false` | | Scheduled jobs run one repair pass when anomalies are found |

### Codex CLI Integration

Section `codex_integration`, object `codex_integration_config`. `enabled` and `codex_weight` are read for each
dispatched job.

| Setting | Default | Allowed | Restart | Notes |
|---------|---------|---------|---------|-------|
| `enabled` | `false` | `true`, `false` | | |
| `credential_mode` | `none` | `none`, `api_key`, `subscription` | yes | |
| `api_key` | empty | text | yes | Write-only; used with `api_key` mode |
| `lcp_url` | empty | URL | yes | Credentials provider, used with `subscription` mode |
| `lcp_vendor` | `openai` | text | yes | |
| `codex_weight` | `0.5` | 0.0 to 1.0 | | Share of dispatched analysis jobs sent to Codex instead of Claude |

### cidx-meta Backup

Section `cidx_meta_backup`, object `cidx_meta_backup_config`. Behaviour:
[Dependency Map: cidx-meta backup mirror](../architecture/dependency-map.md#cidx-meta-backup-mirror).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `enabled` | `false` | `true`, `false` | Runs the backup sync before cidx-meta is indexed |
| `remote_url` | empty | git URL | |

### GitHub/GitLab Keys

Saved through `/admin/config/api-keys/github` and `/admin/config/api-keys/gitlab`, not the section form; stored by
the CI token manager, not in `ServerConfig`. Users of the stored GitHub token include GitHub repository discovery,
the Research Assistant and self-monitoring. GitLab also takes an `api_url` for a self-hosted instance.

## Observability

### OpenTelemetry Export

Section `telemetry`, object `telemetry_config`. Every field is read once at startup. See
[Observability: OpenTelemetry](../server/observability.md#opentelemetry).

| Setting | Default | Allowed | Restart |
|---------|---------|---------|---------|
| `enabled` | `false` | `true`, `false` | yes |
| `collector_endpoint` | `http://localhost:4317` | URL | yes |
| `collector_protocol` | `grpc` | `grpc`, `http` | yes |
| `service_name` | `cidx-server` | text | yes |
| `export_traces` | `true` | `true`, `false` | yes |
| `export_metrics` | `true` | `true`, `false` | yes |
| `export_logs` | `false` | `true`, `false` | yes |
| `machine_metrics_enabled` | `true` | `true`, `false` | yes |
| `machine_metrics_interval_seconds` | `60` | 1 or more | yes |
| `trace_sample_rate` | `1.0` | 0.0 to 1.0 | yes |
| `deployment_environment` | `development` | `development`, `staging`, `production` (form) | yes |

### Langfuse Trace Export

Section `langfuse`, object `langfuse_config`.

| Setting | Default | Allowed | Restart | Notes |
|---------|---------|---------|---------|-------|
| `enabled` | `false` | `true`, `false` | yes | |
| `public_key` | empty | text | yes | |
| `secret_key` | empty | text | yes | Write-only |
| `host` | `https://cloud.langfuse.com` | `http://` or `https://` URL | | |
| `auto_trace_enabled` | `false` | `true`, `false` | yes | Creates a trace on the first tool call when the client has not started one |

### Langfuse Trace Import

Saved through `POST /admin/config/langfuse_pull`, object `langfuse_config`. The sync re-reads these values on every
cycle. See [Langfuse Trace Sync](../server/langfuse-trace-sync.md).

| Setting | Default | Allowed |
|---------|---------|---------|
| `pull_enabled` | `false` | `true`, `false` |
| `pull_host` | `https://cloud.langfuse.com` | URL; empty saves the default |
| `pull_sync_interval_seconds` | `300` | 60 to 3600 (clamped) |
| `pull_trace_age_days` | `30` | 1 to 365 (clamped) |
| `pull_max_concurrent_observations` | `5` | 1 to 20 (clamped) |
| `pull_projects` | empty list | public key and secret key per project; duplicate public keys are rejected |

## Wiki

Section `wiki`, object `wiki_config`. Read when a wiki page is rendered. See [Wiki](../server/wiki.md).

| Setting | Default | Allowed | Notes |
|---------|---------|---------|-------|
| `enable_header_block_parsing` | `true` | `true`, `false` | Removes leading "Article Number", "Title" and "Publication Status" lines from the article body |
| `enable_article_number` | `true` | `true`, `false` | Shows `article_number` / `original_article` in the metadata panel |
| `enable_publication_status` | `true` | `true`, `false` | Shows `publication_status` in the metadata panel |
| `enable_views_seeding` | `true` | `true`, `false` | Seeds view counts from the front-matter `views` field when the wiki is enabled, and shows that field |
| `metadata_display_order` | empty | comma-separated metadata keys | Listed keys first, the rest alphabetically |

## Settings changed outside the Configuration screen

| Setting | Where |
|---------|-------|
| `self_monitoring_config.enabled`, `cadence_minutes`, `model` | Self-Monitoring page ([Self-Monitoring](../server/self-monitoring.md#configuration)) |
| Provider API keys, subscription mode | The Provider API Keys and Subscription Mode panels, which call their own endpoints (above) |
| GitHub and GitLab tokens | The GitHub/GitLab Keys panel (above) |
| `golden_repos_config.refresh_interval_seconds` | Also the MCP tools `get_global_config` / `set_global_config` |
| `memory_retrieval_config.*` | No front door; see [Memory Retrieval: Settings](../server/memory-retrieval.md#settings) |

## Not editable in the Web UI

These fields exist in `ServerConfig` but the Configuration screen has no control for them. The default applies
unless the stored configuration already carries another value.

| Object | Setting | Default | Server-side range |
|--------|---------|---------|-------------------|
| top level | `query_provider_max_concurrency` | `16` | |
| top level | `coalesce_enabled` | `true` | |
| top level | `coalesce_max_batch_size` | `96` | |
| top level | `coalesce_k_min`, `coalesce_k_max` | `8`, `32` | |
| top level | `snapshot_retention_keep_last` | `3` | |
| top level | `snapshot_min_retention_age_seconds` | `900.0` | |
| top level | `deactivation_query_drain_max_wait_seconds` | `30.0` | |
| top level | `nfs_visibility_timeout_seconds` | `60.0` | |
| top level | `research_session_retention_days` | `7` | |
| `cache_config` | `fts_cache_reload_on_access` | `true` | |
| `cache_config` | `query_path_cache_enabled` | `true` | |
| `cache_config` | `repo_config_cache_ttl_seconds`, `repo_config_cache_max_entries` | `30`, `2048` | |
| `resource_config` | `git_update_index_timeout`, `git_restore_timeout` | `300`, `300` | |
| `scip_config` | `scip_reference_limit` | `100` | 10 to 10000 |
| `scip_config` | `scip_dependency_depth` | `3` | 1 to 10 |
| `scip_config` | `scip_callchain_max_depth` | `3` | 1 to 50 |
| `scip_config` | `scip_callchain_limit` | `100` | 1 to 1000 |
| `scip_config` | `scip_workspace_retention_days` | `7` | 1 to 365 |
| `git_timeouts_config` | `git_local_timeout`, `git_remote_timeout` | `30`, `300` | 5 or more, 30 or more |
| `git_timeouts_config` | `github_api_timeout`, `gitlab_api_timeout` | `30`, `30` | 5 to 120 |
| `error_handling_config` | `max_retry_attempts` | `3` | 1 to 10 |
| `error_handling_config` | `base_retry_delay_seconds`, `max_retry_delay_seconds` | `0.1`, `60.0` | 0.01 to 5.0, 1 to 300 |
| `api_limits_config` | `default_file_read_lines`, `max_file_read_lines` | `500`, `5000` | 100 to 5000, 500 to 50000 |
| `api_limits_config` | `default_diff_lines`, `max_diff_lines` | `500`, `5000` | 100 to 5000, 500 to 50000 |
| `api_limits_config` | `default_log_commits`, `max_log_commits` | `50`, `500` | 10 to 500, 50 to 5000 |
| `api_limits_config` | `audit_log_default_limit` | `100` | 10 to 1000 |
| `api_limits_config` | `log_page_size_default`, `log_page_size_max` | `50`, `500` | 10 to 500, 100 to 5000 |
| `web_security_config` | `web_session_timeout_seconds` | `28800` | 1800 to 86400 |
| `web_security_config` | `admin_session_timeout_seconds` | `3600` | |
| `web_security_config` | `restrict_non_sso_to_web_ui` | `false` | |
| `multi_search_limits_config` | `omni_max_workers`, `omni_per_repo_timeout_seconds` | `10`, `300` | 1 to 100, 1 to 3600 |
| `background_jobs_config` | `max_concurrent_refresh_jobs` | `-1` (half of `max_concurrent_background_jobs`) | |
| `background_jobs_config` | `xray_max_concurrent_jobs` | `20` | |
| `background_jobs_config` | `temporal_lane_concurrency` | `2` | 1 to 32 |
| `background_jobs_config` | `job_admission_memory_gate_enabled`, `job_admission_memory_max_used_pct`, `job_admission_backoff_seconds` | `true`, `80.0`, `2.0` | |
| `claude_integration_config` | `scheduled_catchup_enabled`, `scheduled_catchup_interval_minutes` | `false`, `60` | |
| `claude_integration_config` | `ra_curl_allowed_cidrs` | empty list | |
| `password_security` | `check_common_passwords`, `check_personal_info`, `check_keyboard_patterns`, `check_sequential_chars` | `true` | |
| `password_expiry_config` | `enabled`, `max_age_days` | `false`, `90` | |
| `oidc_provider_config` | `provider_name` | `SSO` | |
| `repository_config` | `enable_pr_creation`, `pr_base_branch`, `default_branch` | `true`, `main`, `main` | |
| `admission_control_config` | `enabled`, `max_inflight_requests`, `retry_after_seconds` | `false`, `100`, `1` | |
| `admission_control_config` | `per_consumer_enabled`, `per_consumer_burst`, `per_consumer_refill_per_second`, `per_consumer_cleanup_seconds` | `false`, `30`, `10.0`, `3600` | |
| `voyage_ai_sinbin`, `cohere_sinbin` | `failure_threshold`, `failure_window_seconds`, `initial_cooldown_seconds`, `max_cooldown_seconds`, `backoff_multiplier` | `5`, `60`, `30`, `300`, `2.0` | |
| `query_orchestration` | `parallel_query_orchestrator_timeout_seconds`, `max_query_latency_budget_seconds` | `20`, `60` | |
| `query_orchestration` | `all_providers_sinbinned_retry_limit`, `provider_health_probe_interval_seconds`, `provider_health_probe_join_timeout_seconds` | `2`, `30`, `5` | |
| `memory_retrieval_config` | five settings | see [Memory Retrieval](../server/memory-retrieval.md#settings) | |

Of these, `coalesce_k_min`, `coalesce_k_max`, `scip_reference_limit`, `scip_dependency_depth`,
`scip_callchain_max_depth`, `scip_callchain_limit`, `scip_workspace_retention_days`, `temporal_lane_concurrency`,
`scheduled_catchup_enabled`, `scheduled_catchup_interval_minutes`, `max_retry_attempts`, `base_retry_delay_seconds`
and `max_retry_delay_seconds` are in `RESTART_REQUIRED_FIELDS`.

Two further fields are written by the server itself, not by an operator: `mcp_self_registration` (the server's own
MCP client credentials) and `alias_lock_config.db_backed_enabled_promoted` (set by a startup migration).

## Bootstrap keys (config.json)

Read at startup from `config.json` in the server data directory; restart the server after changing one. Every
settings save rewrites `config.json` from the bootstrap values the saving process holds, so edit the file and
restart before saving any setting ([Deployment](../server/deployment.md#bootstrap-configuration-configjson)).

| Key | Default | Allowed / content | Purpose |
|-----|---------|-------------------|---------|
| `server_dir` | the server data directory | path | Server data directory |
| `storage_mode` | `sqlite` | `sqlite`, `postgres` | Standalone or cluster storage |
| `postgres_dsn` | none | connection string | PostgreSQL for cluster mode |
| `cluster` | none | `node_id` (empty), `sharding_enabled` (`false`), `shard_replicas` (`1`) | Cluster node identity ([Cluster Setup](../server/cluster-setup.md)) |
| `clone_backend` | `local` | `local`, `ontap`, `cow-daemon` | Clone backend for snapshots and activations |
| `cow_daemon` | none | `daemon_url`, `api_key`, `mount_point`, `poll_interval_seconds` (`2`), `timeout_seconds` (`600`), `daemon_storage_path`, `request_timeout_seconds` (`30`) | Copy-on-write daemon backend ([CoW Storage Setup](../server/cow-storage-setup.md)) |
| `ontap` | none | `endpoint`, `svm_name`, `parent_volume`, `mount_point` (`/mnt/fsx`), `admin_user`, `admin_password`, `nfs_data_lif`, `nfs_export` (`/`) | ONTAP FlexClone backend |
| `pace_maker_clone_path` | none | path | pace-maker checkout, written by the installer and the auto-updater |
| `enable_malloc_arena_max` | `true` | `true`, `false` | glibc memory mitigation |
| `enable_malloc_trim` | `true` | `true`, `false` | glibc memory mitigation |
| `server_threadpool_size` | `256` | integer; 0 or less keeps anyio's default of 40 | Thread pool for synchronous request handlers |
| `mcp_dispatch_pool_size` | `128` | 1 to 1024 | asyncio default executor size |
| `query_executor_pool_size` | `256` | 1 to 2048 | Shared query executor size |
| `enable_predeactivation_leak_scan` | `false` | `true`, `false` | Startup cleanup control |
| `orphan_trash_sweep_per_startup_cap` | `100` | integer | Entries the startup orphan sweep handles |
| `enable_graph_channel_repair` | `true` | `true`, `false` | Dependency-map graph-channel repair |
| `graph_repair_self_loop`, `graph_repair_malformed_yaml`, `graph_repair_garbage_domain`, `graph_repair_bidirectional_mismatch` | none (`dry_run`) | `disabled`, `dry_run`, `enabled` | Per-anomaly repair mode ([Dependency Map](../architecture/dependency-map.md#repair-and-phase-37-graph-channel-repair)) |
| `fault_injection_enabled`, `fault_injection_nonprod_ack` | `false`, `false` | `true`, `false` | Fault-injection harness gate ([Fault Injection](../server/fault-injection.md)) |
