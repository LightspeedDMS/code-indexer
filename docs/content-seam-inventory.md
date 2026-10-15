# Content seam inventory (generated -- do not edit)

Every filesystem access the cidx server pod can reach, from
`tests/unit/server/content_seam_inventory.json`. Regenerate with
`PYTHONPATH=src python3 -m tests.unit.server.test_content_seam_inventory --write`.
The repo mount can come off when no row is in an open class (unclassified, repo-read, repo-write, cidx-state-in-repo).

| Class | Rows |
|---|---|
| unclassified | 0 |
| repo-read | 133 |
| repo-write | 14 |
| cidx-state-in-repo | 936 |
| migrated | 426 |
| refused | 4 |
| local-only | 466 |
| cidx-state | 781 |
| not-repo | 440 |

## Modules with open rows

| Module | unclassified | repo-read | repo-write | cidx-state-in-repo |
|---|---|---|---|---|
| `backends/filesystem_backend.py` | 0 | 0 | 0 | 7 |
| `cli.py` | 0 | 2 | 0 | 23 |
| `cli_daemon_delegation.py` | 0 | 0 | 0 | 1 |
| `cli_scip.py` | 0 | 0 | 0 | 2 |
| `config.py` | 0 | 0 | 0 | 22 |
| `daemon/socket_helper.py` | 0 | 0 | 0 | 2 |
| `global_repos/alias_manager.py` | 0 | 0 | 0 | 2 |
| `global_repos/control_plane_metadata.py` | 0 | 0 | 0 | 10 |
| `global_repos/directory_explorer.py` | 0 | 5 | 0 | 0 |
| `global_repos/frontmatter_verifier.py` | 0 | 0 | 0 | 2 |
| `global_repos/git_operations.py` | 0 | 5 | 0 | 0 |
| `global_repos/refresh_integrity_gate.py` | 0 | 0 | 0 | 7 |
| `global_repos/refresh_scheduler.py` | 0 | 0 | 0 | 15 |
| `global_repos/regex_search.py` | 0 | 0 | 0 | 1 |
| `global_repos/snapshot_reader_lease.py` | 0 | 0 | 0 | 2 |
| `global_repos/trigram_index_manager.py` | 0 | 0 | 0 | 10 |
| `mode_detection/command_mode_detector.py` | 0 | 0 | 0 | 6 |
| `mode_specific_handlers.py` | 0 | 0 | 0 | 1 |
| `proxy/config_manager.py` | 0 | 0 | 0 | 1 |
| `proxy/proxy_initializer.py` | 0 | 4 | 0 | 6 |
| `scip/database/builder.py` | 0 | 0 | 0 | 3 |
| `scip/database/migration.py` | 0 | 0 | 0 | 5 |
| `scip/database/schema.py` | 0 | 0 | 0 | 5 |
| `scip/database/verify.py` | 0 | 0 | 0 | 3 |
| `scip/discovery.py` | 0 | 4 | 0 | 0 |
| `scip/generator.py` | 0 | 0 | 0 | 5 |
| `scip/indexers/csharp.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/delegated.py` | 0 | 1 | 0 | 3 |
| `scip/indexers/go.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/java.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/plugin_backend.py` | 0 | 0 | 0 | 5 |
| `scip/indexers/python.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/typescript.py` | 0 | 0 | 0 | 1 |
| `scip/query/backends.py` | 0 | 1 | 0 | 1 |
| `scip/query/composites.py` | 0 | 0 | 0 | 8 |
| `scip/query/loader.py` | 0 | 0 | 0 | 3 |
| `scip/query/primitives.py` | 0 | 0 | 0 | 3 |
| `scip/status.py` | 0 | 0 | 0 | 4 |
| `server/app_helpers.py` | 0 | 4 | 0 | 0 |
| `server/auto_update/deployment_executor.py` | 0 | 2 | 2 | 0 |
| `server/cache/fts_index_cache.py` | 0 | 0 | 0 | 5 |
| `server/cache/hnsw_index_cache.py` | 0 | 0 | 0 | 4 |
| `server/cache/id_index_cache.py` | 0 | 0 | 0 | 3 |
| `server/managers/composite_file_listing.py` | 0 | 6 | 0 | 0 |
| `server/mcp/handlers/_legacy.py` | 0 | 1 | 0 | 0 |
| `server/mcp/handlers/_provider_temporal_job.py` | 0 | 0 | 0 | 1 |
| `server/mcp/handlers/repos.py` | 0 | 0 | 0 | 3 |
| `server/mcp/handlers/search/regex_search.py` | 0 | 1 | 0 | 0 |
| `server/mcp/memory_retrieval_pipeline.py` | 0 | 0 | 0 | 2 |
| `server/multi/multi_search_service.py` | 0 | 0 | 0 | 1 |
| `server/multi/scip_multi_service.py` | 0 | 0 | 0 | 1 |
| `server/query/semantic_query_manager.py` | 0 | 0 | 0 | 4 |
| `server/repositories/activated_repo_manager.py` | 0 | 0 | 0 | 8 |
| `server/repositories/golden_repo_manager.py` | 0 | 0 | 0 | 25 |
| `server/repositories/repository_listing_manager.py` | 0 | 3 | 0 | 0 |
| `server/routers/activated_repos.py` | 0 | 2 | 0 | 7 |
| `server/routers/git.py` | 0 | 4 | 0 | 0 |
| `server/routers/inline_query.py` | 0 | 0 | 0 | 1 |
| `server/routers/inline_repos.py` | 0 | 11 | 0 | 0 |
| `server/routers/inline_repos_v2.py` | 0 | 8 | 0 | 0 |
| `server/routers/repository_health.py` | 0 | 2 | 0 | 6 |
| `server/services/activated_repo_index_manager.py` | 0 | 5 | 0 | 21 |
| `server/services/auto_watch_manager.py` | 0 | 0 | 0 | 1 |
| `server/services/backfill_journal_service.py` | 0 | 0 | 0 | 6 |
| `server/services/cidx_meta_backup/branch_detect.py` | 0 | 0 | 0 | 1 |
| `server/services/config_seeding.py` | 0 | 0 | 0 | 2 |
| `server/services/config_service.py` | 0 | 0 | 0 | 7 |
| `server/services/dashboard_service.py` | 0 | 0 | 0 | 3 |
| `server/services/description_refresh_scheduler.py` | 0 | 0 | 0 | 9 |
| `server/services/diagnostics_service.py` | 0 | 0 | 0 | 5 |
| `server/services/file_crud_service.py` | 0 | 1 | 11 | 0 |
| `server/services/file_service.py` | 0 | 11 | 0 | 1 |
| `server/services/fleet_migration/completion_gate.py` | 0 | 0 | 0 | 12 |
| `server/services/fleet_migration/discovery.py` | 0 | 0 | 0 | 4 |
| `server/services/fleet_migration/orchestrator.py` | 0 | 0 | 0 | 4 |
| `server/services/fleet_migration/quarantine.py` | 0 | 0 | 0 | 4 |
| `server/services/fts_index_builder.py` | 0 | 0 | 0 | 5 |
| `server/services/git_operations_service.py` | 0 | 21 | 0 | 2 |
| `server/services/golden_index_backfill.py` | 0 | 0 | 0 | 4 |
| `server/services/hnsw_orphan_sweep/discovery.py` | 0 | 1 | 0 | 3 |
| `server/services/hnsw_orphan_sweep/repair_executor.py` | 0 | 0 | 0 | 5 |
| `server/services/index_freshness.py` | 0 | 0 | 0 | 3 |
| `server/services/langfuse_trace_sync_service.py` | 0 | 0 | 0 | 1 |
| `server/services/metadata_outbox/sinks.py` | 0 | 0 | 0 | 2 |
| `server/services/metadata_reader.py` | 0 | 0 | 0 | 3 |
| `server/services/provider_index_service.py` | 0 | 0 | 0 | 6 |
| `server/services/repository_health_aggregator.py` | 0 | 0 | 0 | 12 |
| `server/services/scip_lifecycle.py` | 0 | 0 | 0 | 7 |
| `server/services/scip_query_service.py` | 0 | 0 | 0 | 1 |
| `server/services/semantic_backfill/policy.py` | 0 | 0 | 0 | 8 |
| `server/services/stats_service.py` | 0 | 6 | 0 | 1 |
| `server/services/temporal_legacy_migration/mover.py` | 0 | 0 | 0 | 54 |
| `server/services/temporal_legacy_migration/verification.py` | 0 | 0 | 0 | 15 |
| `server/services/versioned_snapshot_reclaim.py` | 0 | 0 | 0 | 1 |
| `server/startup/bootstrap.py` | 0 | 0 | 0 | 6 |
| `server/startup/claude_cli_startup.py` | 0 | 0 | 0 | 1 |
| `server/startup/lifespan.py` | 0 | 0 | 0 | 5 |
| `server/startup/nfs_self_check.py` | 0 | 0 | 0 | 6 |
| `server/validators/composite_repo_validator.py` | 0 | 0 | 0 | 2 |
| `server/web/routes.py` | 0 | 0 | 0 | 9 |
| `server/wiki/routes.py` | 0 | 10 | 0 | 0 |
| `server/wiki/wiki_cache.py` | 0 | 3 | 0 | 0 |
| `server/wiki/wiki_service.py` | 0 | 3 | 0 | 0 |
| `services/chunk_migration_cli.py` | 0 | 0 | 0 | 14 |
| `services/config_fixer.py` | 0 | 0 | 0 | 13 |
| `services/generic_query_service.py` | 0 | 4 | 0 | 0 |
| `services/git_detection.py` | 0 | 0 | 0 | 6 |
| `services/hnsw_health_service.py` | 0 | 0 | 0 | 8 |
| `services/indexing_lock.py` | 0 | 0 | 0 | 9 |
| `services/indexing_progress_log.py` | 0 | 0 | 0 | 8 |
| `services/json_validator.py` | 0 | 0 | 0 | 3 |
| `services/language_mapper.py` | 0 | 0 | 0 | 4 |
| `services/multi_index_query_service.py` | 0 | 0 | 0 | 4 |
| `services/progressive_metadata.py` | 0 | 0 | 0 | 11 |
| `services/provider_health_bridge.py` | 0 | 0 | 0 | 13 |
| `services/provider_rebuild_check.py` | 0 | 0 | 0 | 2 |
| `services/smart_indexer.py` | 0 | 0 | 0 | 2 |
| `services/tantivy_index_manager.py` | 0 | 0 | 0 | 17 |
| `services/temporal/temporal_blank_out.py` | 0 | 0 | 0 | 4 |
| `services/temporal/temporal_collection_naming.py` | 0 | 0 | 0 | 12 |
| `services/temporal/temporal_indexer.py` | 0 | 0 | 0 | 5 |
| `services/temporal/temporal_migration.py` | 0 | 0 | 0 | 24 |
| `services/temporal/temporal_progressive_metadata.py` | 0 | 0 | 0 | 10 |
| `services/temporal/temporal_projection_matrix.py` | 0 | 0 | 0 | 10 |
| `services/temporal/temporal_reconciliation.py` | 0 | 0 | 0 | 1 |
| `services/temporal/temporal_row_existence.py` | 0 | 0 | 0 | 2 |
| `services/temporal/temporal_status.py` | 0 | 0 | 0 | 5 |
| `services/temporal/temporal_structure_marker.py` | 0 | 0 | 0 | 5 |
| `services/vector_calculation_manager.py` | 0 | 0 | 0 | 1 |
| `services/watch_metadata.py` | 0 | 0 | 0 | 4 |
| `storage/background_index_rebuilder.py` | 0 | 0 | 0 | 12 |
| `storage/filesystem_vector_store.py` | 0 | 0 | 0 | 74 |
| `storage/hnsw_index_manager.py` | 0 | 0 | 0 | 47 |
| `storage/id_index_manager.py` | 0 | 0 | 0 | 9 |
| `storage/projection_matrix_manager.py` | 0 | 0 | 0 | 2 |
| `storage/shared/chunk_layout.py` | 0 | 0 | 0 | 7 |
| `storage/shared/chunk_store_cache.py` | 0 | 0 | 0 | 1 |
| `storage/shared/collection_dedup_repair.py` | 0 | 0 | 0 | 35 |
| `storage/shared/collection_meta_cache.py` | 0 | 0 | 0 | 3 |
| `storage/shared/collection_migration.py` | 0 | 0 | 0 | 34 |
| `storage/shared/hnsw_sync_state.py` | 0 | 0 | 0 | 9 |
| `storage/sqlite_chunk_store.py` | 0 | 0 | 0 | 4 |
| `storage/temporal_metadata_sqlite_backend.py` | 0 | 0 | 0 | 2 |
| `storage/temporal_metadata_store.py` | 0 | 0 | 0 | 4 |
| `utils/exception_logger.py` | 0 | 0 | 0 | 3 |
| `utils/file_locking.py` | 0 | 0 | 0 | 1 |
| `utils/git_runner.py` | 0 | 1 | 0 | 0 |
| `utils/log_path_helper.py` | 0 | 0 | 0 | 1 |
| `utils/path_confinement.py` | 0 | 1 | 1 | 0 |
| `utils/yaml_utils.py` | 0 | 0 | 0 | 5 |

## Local git and .git probes still open in the indexing and query paths

Ratcheted (see the test's docstring): this list may only shrink.

| Module | Function | Op | Note |
|---|---|---|---|
| `services/generic_query_service.py` | `GenericQueryService._get_current_branch_context` | `run_git_command ls-tree` | GenericQueryService is constructed only in cli.py `cidx query`, which the server spawns only for composite (activated) repositories (SemanticQueryManager._execute_cli_query); pinned by test_unreached_repo_reads_stay_unreached |
| `services/generic_query_service.py` | `GenericQueryService._get_current_branch_context` | `run_git_command rev-parse` | GenericQueryService is constructed only in cli.py `cidx query`, which the server spawns only for composite (activated) repositories (SemanticQueryManager._execute_cli_query); pinned by test_unreached_repo_reads_stay_unreached |
| `services/generic_query_service.py` | `GenericQueryService._is_commit_reachable` | `run_git_command merge-base` | GenericQueryService is constructed only in cli.py `cidx query`, which the server spawns only for composite (activated) repositories (SemanticQueryManager._execute_cli_query); pinned by test_unreached_repo_reads_stay_unreached |
