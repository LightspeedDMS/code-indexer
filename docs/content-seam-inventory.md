# Content seam inventory (generated -- do not edit)

Every filesystem access the cidx server pod can reach, from
`tests/unit/server/content_seam_inventory.json`. Regenerate with
`PYTHONPATH=src python3 -m tests.unit.server.test_content_seam_inventory --write`.
The repo mount can come off when no row is in an open class (unclassified, repo-read, repo-write, cidx-state-in-repo).

| Class | Rows |
|---|---|
| unclassified | 0 |
| repo-read | 452 |
| repo-write | 93 |
| cidx-state-in-repo | 1325 |
| migrated | 142 |
| local-only | 179 |
| cidx-state | 526 |
| not-repo | 414 |

## Modules with open rows

| Module | unclassified | repo-read | repo-write | cidx-state-in-repo |
|---|---|---|---|---|
| `backends/filesystem_backend.py` | 0 | 0 | 0 | 7 |
| `cli.py` | 0 | 4 | 0 | 22 |
| `cli_daemon_delegation.py` | 0 | 0 | 0 | 1 |
| `cli_scip.py` | 0 | 0 | 0 | 2 |
| `config.py` | 0 | 2 | 0 | 22 |
| `daemon/socket_helper.py` | 0 | 2 | 0 | 2 |
| `daemon/watch_manager.py` | 0 | 1 | 0 | 0 |
| `global_repos/alias_manager.py` | 0 | 1 | 0 | 25 |
| `global_repos/cleanup_manager.py` | 0 | 2 | 5 | 0 |
| `global_repos/dependency_map_analyzer.py` | 0 | 0 | 0 | 35 |
| `global_repos/directory_explorer.py` | 0 | 5 | 0 | 0 |
| `global_repos/frontmatter_verifier.py` | 0 | 0 | 0 | 2 |
| `global_repos/git_operations.py` | 0 | 14 | 0 | 0 |
| `global_repos/git_pull_updater.py` | 0 | 6 | 3 | 0 |
| `global_repos/global_registry.py` | 0 | 0 | 1 | 5 |
| `global_repos/lifecycle_batch_runner.py` | 0 | 0 | 0 | 8 |
| `global_repos/lifecycle_claude_cli_invoker.py` | 0 | 2 | 0 | 0 |
| `global_repos/meta_description_hook.py` | 0 | 1 | 0 | 9 |
| `global_repos/meta_directory_updater.py` | 0 | 0 | 0 | 6 |
| `global_repos/refresh_integrity_gate.py` | 0 | 0 | 0 | 7 |
| `global_repos/refresh_scheduler.py` | 0 | 8 | 3 | 21 |
| `global_repos/regex_search.py` | 0 | 3 | 0 | 1 |
| `global_repos/repo_analyzer.py` | 0 | 7 | 0 | 0 |
| `global_repos/shared_operations.py` | 0 | 0 | 1 | 0 |
| `global_repos/snapshot_reader_lease.py` | 0 | 0 | 0 | 19 |
| `global_repos/snapshot_retention.py` | 0 | 0 | 0 | 3 |
| `global_repos/trigram_index_manager.py` | 0 | 2 | 0 | 10 |
| `global_repos/write_lock_manager.py` | 0 | 0 | 0 | 19 |
| `indexing/file_finder.py` | 0 | 2 | 0 | 0 |
| `indexing/fixed_size_chunker.py` | 0 | 1 | 0 | 0 |
| `indexing/image_extractor.py` | 0 | 5 | 0 | 0 |
| `indexing/processor.py` | 0 | 1 | 0 | 0 |
| `mode_detection/command_mode_detector.py` | 0 | 0 | 0 | 6 |
| `mode_specific_handlers.py` | 0 | 0 | 0 | 1 |
| `proxy/config_manager.py` | 0 | 3 | 0 | 1 |
| `proxy/proxy_initializer.py` | 0 | 4 | 0 | 6 |
| `proxy/repository_formatter.py` | 0 | 2 | 0 | 0 |
| `scip/database/builder.py` | 0 | 0 | 0 | 3 |
| `scip/database/migration.py` | 0 | 0 | 0 | 5 |
| `scip/database/schema.py` | 0 | 0 | 0 | 5 |
| `scip/database/verify.py` | 0 | 0 | 0 | 3 |
| `scip/discovery.py` | 0 | 5 | 0 | 0 |
| `scip/generator.py` | 0 | 0 | 0 | 5 |
| `scip/indexers/csharp.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/delegated.py` | 0 | 1 | 0 | 3 |
| `scip/indexers/go.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/java.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/python.py` | 0 | 0 | 0 | 1 |
| `scip/indexers/typescript.py` | 0 | 0 | 0 | 1 |
| `scip/query/backends.py` | 0 | 1 | 0 | 1 |
| `scip/query/composites.py` | 0 | 0 | 0 | 8 |
| `scip/query/loader.py` | 0 | 0 | 0 | 3 |
| `scip/query/primitives.py` | 0 | 0 | 0 | 3 |
| `scip/status.py` | 0 | 0 | 0 | 4 |
| `server/app_helpers.py` | 0 | 4 | 0 | 2 |
| `server/auto_update/deployment_executor.py` | 0 | 4 | 16 | 0 |
| `server/cache/fts_index_cache.py` | 0 | 0 | 0 | 5 |
| `server/cache/hnsw_index_cache.py` | 0 | 0 | 0 | 4 |
| `server/cache/id_index_cache.py` | 0 | 0 | 0 | 3 |
| `server/git/git_sync_executor.py` | 0 | 11 | 9 | 0 |
| `server/lifecycle/global_repos_lifecycle.py` | 0 | 0 | 1 | 0 |
| `server/managers/composite_file_listing.py` | 0 | 6 | 0 | 0 |
| `server/mcp/handlers/_legacy.py` | 0 | 4 | 0 | 3 |
| `server/mcp/handlers/_provider_temporal_job.py` | 0 | 0 | 0 | 1 |
| `server/mcp/handlers/depmap.py` | 0 | 0 | 0 | 3 |
| `server/mcp/handlers/files.py` | 0 | 1 | 0 | 6 |
| `server/mcp/handlers/guides.py` | 0 | 0 | 0 | 3 |
| `server/mcp/handlers/repos.py` | 0 | 5 | 0 | 4 |
| `server/mcp/handlers/search/regex_search.py` | 0 | 1 | 0 | 0 |
| `server/mcp/handlers/search/repo_search.py` | 0 | 1 | 0 | 0 |
| `server/mcp/handlers/xray/_dump_ast.py` | 0 | 4 | 0 | 0 |
| `server/mcp/handlers/xray_graph/_candidates.py` | 0 | 1 | 0 | 0 |
| `server/mcp/memory_retrieval_pipeline.py` | 0 | 0 | 0 | 2 |
| `server/multi/multi_search_service.py` | 0 | 1 | 0 | 1 |
| `server/multi/scip_multi_service.py` | 0 | 1 | 0 | 1 |
| `server/query/semantic_query_manager.py` | 0 | 0 | 0 | 4 |
| `server/repositories/activated_repo_manager.py` | 0 | 50 | 9 | 10 |
| `server/repositories/deactivation_helpers.py` | 0 | 5 | 4 | 0 |
| `server/repositories/golden_repo_manager.py` | 0 | 23 | 9 | 32 |
| `server/repositories/repository_listing_manager.py` | 0 | 8 | 0 | 0 |
| `server/repositories/user_dir_permissions.py` | 0 | 2 | 4 | 0 |
| `server/routers/activated_repos.py` | 0 | 8 | 0 | 7 |
| `server/routers/diagnostics.py` | 0 | 0 | 0 | 4 |
| `server/routers/git.py` | 0 | 4 | 0 | 0 |
| `server/routers/inline_query.py` | 0 | 0 | 0 | 1 |
| `server/routers/inline_repos.py` | 0 | 11 | 0 | 0 |
| `server/routers/inline_repos_v2.py` | 0 | 8 | 0 | 0 |
| `server/routers/repository_health.py` | 0 | 2 | 0 | 9 |
| `server/services/activated_repo_index_manager.py` | 0 | 5 | 0 | 21 |
| `server/services/activity_journal_service.py` | 0 | 0 | 0 | 13 |
| `server/services/alias_lock_store/factory.py` | 0 | 1 | 0 | 0 |
| `server/services/auto_watch_manager.py` | 0 | 1 | 0 | 1 |
| `server/services/backfill_journal_service.py` | 0 | 0 | 0 | 6 |
| `server/services/branch_service.py` | 0 | 1 | 0 | 0 |
| `server/services/cache_invalidation_service.py` | 0 | 0 | 0 | 4 |
| `server/services/cidx_meta_backup/bootstrap.py` | 0 | 0 | 0 | 5 |
| `server/services/cidx_meta_backup/branch_detect.py` | 0 | 0 | 0 | 1 |
| `server/services/cidx_meta_backup/sync.py` | 0 | 0 | 0 | 1 |
| `server/services/claude_cli_manager.py` | 0 | 0 | 0 | 3 |
| `server/services/config_seeding.py` | 0 | 0 | 0 | 2 |
| `server/services/config_service.py` | 0 | 0 | 0 | 6 |
| `server/services/dashboard_service.py` | 0 | 6 | 0 | 3 |
| `server/services/dep_map_delta_journal.py` | 0 | 0 | 0 | 5 |
| `server/services/dep_map_file_utils.py` | 0 | 0 | 0 | 3 |
| `server/services/dep_map_health_detector.py` | 0 | 0 | 0 | 13 |
| `server/services/dep_map_index_regenerator.py` | 0 | 0 | 0 | 4 |
| `server/services/dep_map_mcp_parser.py` | 0 | 0 | 0 | 13 |
| `server/services/dep_map_parser_graph.py` | 0 | 0 | 0 | 4 |
| `server/services/dep_map_repair_bidirectional.py` | 0 | 0 | 0 | 1 |
| `server/services/dep_map_repair_bidirectional_backfill.py` | 0 | 0 | 0 | 1 |
| `server/services/dep_map_repair_executor.py` | 0 | 0 | 0 | 17 |
| `server/services/dep_map_repair_garbage_domain.py` | 0 | 0 | 0 | 4 |
| `server/services/dep_map_repair_malformed_yaml.py` | 0 | 0 | 0 | 3 |
| `server/services/dep_map_repair_phase37.py` | 0 | 0 | 0 | 5 |
| `server/services/dependency_map_dashboard_service.py` | 0 | 0 | 0 | 2 |
| `server/services/dependency_map_domain_service.py` | 0 | 0 | 0 | 10 |
| `server/services/dependency_map_service.py` | 0 | 4 | 0 | 58 |
| `server/services/description_refresh_scheduler.py` | 0 | 0 | 0 | 15 |
| `server/services/diagnostics_service.py` | 0 | 1 | 0 | 5 |
| `server/services/file_crud_service.py` | 0 | 1 | 11 | 1 |
| `server/services/file_service.py` | 0 | 11 | 0 | 1 |
| `server/services/fleet_migration/completion_gate.py` | 0 | 0 | 0 | 12 |
| `server/services/fleet_migration/discovery.py` | 0 | 2 | 0 | 4 |
| `server/services/fleet_migration/orchestrator.py` | 0 | 1 | 0 | 4 |
| `server/services/fleet_migration/quarantine.py` | 0 | 0 | 0 | 4 |
| `server/services/fts_index_builder.py` | 0 | 0 | 0 | 5 |
| `server/services/git_argv_safety.py` | 0 | 1 | 0 | 0 |
| `server/services/git_operations_service.py` | 0 | 24 | 0 | 2 |
| `server/services/git_state_manager.py` | 0 | 5 | 0 | 0 |
| `server/services/golden_index_backfill.py` | 0 | 2 | 0 | 4 |
| `server/services/golden_repo_branch_service.py` | 0 | 5 | 0 | 0 |
| `server/services/golden_repo_reconciler.py` | 0 | 4 | 0 | 0 |
| `server/services/hnsw_orphan_sweep/discovery.py` | 0 | 2 | 0 | 3 |
| `server/services/hnsw_orphan_sweep/repair_executor.py` | 0 | 0 | 0 | 5 |
| `server/services/index_freshness.py` | 0 | 1 | 0 | 3 |
| `server/services/langfuse_readme_generator.py` | 0 | 8 | 3 | 0 |
| `server/services/langfuse_trace_sync_service.py` | 0 | 4 | 6 | 9 |
| `server/services/memory_io.py` | 0 | 0 | 0 | 8 |
| `server/services/memory_store_service.py` | 0 | 0 | 0 | 1 |
| `server/services/memory_store_service_factory.py` | 0 | 0 | 0 | 1 |
| `server/services/metadata_reader.py` | 0 | 0 | 0 | 3 |
| `server/services/provider_index_service.py` | 0 | 0 | 0 | 6 |
| `server/services/query_analytics_export_service.py` | 0 | 0 | 0 | 3 |
| `server/services/repository_health_aggregator.py` | 0 | 0 | 0 | 12 |
| `server/services/scip_lifecycle.py` | 0 | 0 | 0 | 7 |
| `server/services/scip_query_service.py` | 0 | 4 | 0 | 1 |
| `server/services/search_service.py` | 0 | 4 | 0 | 0 |
| `server/services/semantic_backfill/policy.py` | 0 | 3 | 0 | 8 |
| `server/services/shared_job_sentinel.py` | 0 | 0 | 0 | 7 |
| `server/services/stats_service.py` | 0 | 7 | 0 | 1 |
| `server/services/temporal_legacy_migration/discovery.py` | 0 | 3 | 0 | 0 |
| `server/services/temporal_legacy_migration/mover.py` | 0 | 0 | 0 | 54 |
| `server/services/temporal_legacy_migration/verification.py` | 0 | 0 | 0 | 15 |
| `server/services/versioned_snapshot_reclaim.py` | 0 | 1 | 0 | 1 |
| `server/services/versioned_snapshot_reconciler.py` | 0 | 0 | 0 | 2 |
| `server/services/xray_pattern_service.py` | 0 | 0 | 0 | 10 |
| `server/startup/bootstrap.py` | 0 | 3 | 0 | 6 |
| `server/startup/claude_cli_startup.py` | 0 | 0 | 0 | 1 |
| `server/startup/clone_backend_wiring.py` | 0 | 3 | 0 | 0 |
| `server/startup/lifespan.py` | 0 | 1 | 0 | 7 |
| `server/startup/nfs_self_check.py` | 0 | 0 | 0 | 6 |
| `server/storage/filesystem_backends.py` | 0 | 0 | 0 | 5 |
| `server/storage/migration_service.py` | 0 | 0 | 0 | 6 |
| `server/storage/shared/nfs_validator.py` | 0 | 1 | 0 | 7 |
| `server/utils/cow_utils.py` | 0 | 0 | 3 | 0 |
| `server/validators/composite_repo_validator.py` | 0 | 0 | 0 | 2 |
| `server/web/dependency_map_routes.py` | 0 | 0 | 0 | 4 |
| `server/web/routes.py` | 0 | 0 | 0 | 11 |
| `server/wiki/routes.py` | 0 | 19 | 0 | 0 |
| `server/wiki/wiki_cache.py` | 0 | 4 | 0 | 0 |
| `server/wiki/wiki_service.py` | 0 | 5 | 0 | 0 |
| `services/chunk_migration_cli.py` | 0 | 0 | 0 | 14 |
| `services/cohere_multimodal.py` | 0 | 1 | 0 | 0 |
| `services/config_fixer.py` | 0 | 5 | 0 | 13 |
| `services/file_chunking_manager.py` | 0 | 2 | 0 | 0 |
| `services/file_identifier.py` | 0 | 1 | 0 | 0 |
| `services/generic_query_service.py` | 0 | 4 | 0 | 0 |
| `services/git_detection.py` | 0 | 0 | 0 | 6 |
| `services/git_hook_manager.py` | 0 | 8 | 5 | 0 |
| `services/hnsw_health_service.py` | 0 | 0 | 0 | 8 |
| `services/indexing_lock.py` | 0 | 0 | 0 | 9 |
| `services/indexing_progress_log.py` | 0 | 0 | 0 | 8 |
| `services/json_validator.py` | 0 | 0 | 0 | 3 |
| `services/language_mapper.py` | 0 | 0 | 0 | 4 |
| `services/multi_index_query_service.py` | 0 | 0 | 0 | 4 |
| `services/multimodal_utils.py` | 0 | 2 | 0 | 0 |
| `services/progress_subprocess_runner.py` | 0 | 3 | 0 | 0 |
| `services/progressive_metadata.py` | 0 | 0 | 0 | 11 |
| `services/provider_health_bridge.py` | 0 | 0 | 0 | 13 |
| `services/provider_rebuild_check.py` | 0 | 0 | 0 | 2 |
| `services/smart_indexer.py` | 0 | 8 | 0 | 2 |
| `services/tantivy_index_manager.py` | 0 | 0 | 0 | 17 |
| `services/temporal/temporal_blank_out.py` | 0 | 0 | 0 | 4 |
| `services/temporal/temporal_collection_naming.py` | 0 | 0 | 0 | 12 |
| `services/temporal/temporal_indexer.py` | 0 | 0 | 0 | 5 |
| `services/temporal/temporal_migration.py` | 0 | 0 | 0 | 24 |
| `services/temporal/temporal_progressive_metadata.py` | 0 | 0 | 0 | 10 |
| `services/temporal/temporal_projection_matrix.py` | 0 | 0 | 0 | 10 |
| `services/temporal/temporal_reconciliation.py` | 0 | 0 | 0 | 1 |
| `services/temporal/temporal_row_existence.py` | 0 | 0 | 0 | 2 |
| `services/temporal/temporal_server_paths.py` | 0 | 0 | 0 | 1 |
| `services/temporal/temporal_status.py` | 0 | 0 | 0 | 5 |
| `services/temporal/temporal_structure_marker.py` | 0 | 0 | 0 | 5 |
| `services/vector_calculation_manager.py` | 0 | 0 | 0 | 1 |
| `services/watch_metadata.py` | 0 | 0 | 0 | 4 |
| `storage/background_index_rebuilder.py` | 0 | 0 | 0 | 12 |
| `storage/filesystem_vector_store.py` | 0 | 3 | 0 | 74 |
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
| `utils/blob_hash.py` | 0 | 1 | 0 | 0 |
| `utils/exception_logger.py` | 0 | 0 | 0 | 3 |
| `utils/file_locking.py` | 0 | 0 | 0 | 1 |
| `utils/git_runner.py` | 0 | 5 | 0 | 0 |
| `utils/log_path_helper.py` | 0 | 0 | 0 | 1 |
| `utils/path_confinement.py` | 0 | 3 | 0 | 0 |
| `utils/yaml_utils.py` | 0 | 0 | 0 | 5 |
| `xray/ast_engine.py` | 0 | 1 | 0 | 0 |
| `xray/rust_backend.py` | 0 | 2 | 0 | 0 |
| `xray/search_engine.py` | 0 | 5 | 0 | 0 |

## Local git and .git probes still open in the indexing and query paths

Ratcheted (see the test's docstring): this list may only shrink.

| Module | Function | Op | Note |
|---|---|---|---|
| `services/config_fixer.py` | `GitStateDetector.detect_git_state` | `git branch` | OPEN: snapshot fix-config writes git_available=False/unknown into the SNAPSHOT metadata (drift check reads the source); follow-up: give fix-config the git seam |
| `services/config_fixer.py` | `GitStateDetector.detect_git_state` | `git rev-parse` | OPEN: snapshot fix-config writes git_available=False/unknown into the SNAPSHOT metadata (drift check reads the source); follow-up: give fix-config the git seam |
| `services/config_fixer.py` | `GitStateDetector.detect_git_state` | `git status` | OPEN: snapshot fix-config writes git_available=False/unknown into the SNAPSHOT metadata (drift check reads the source); follow-up: give fix-config the git seam |
| `services/generic_query_service.py` | `GenericQueryService._get_current_branch_context` | `run_git_command ls-tree` | OPEN, CLI query only (server reaches it via composite cidx query children); without .git FileIdentifier.git_available is False so results pass unfiltered, never dropped; follow-up: give cidx query the git seam |
| `services/generic_query_service.py` | `GenericQueryService._get_current_branch_context` | `run_git_command rev-parse` | OPEN, CLI query only (server reaches it via composite cidx query children); without .git FileIdentifier.git_available is False so results pass unfiltered, never dropped; follow-up: give cidx query the git seam |
| `services/generic_query_service.py` | `GenericQueryService._is_commit_reachable` | `run_git_command merge-base` | OPEN, CLI query only; reached only when git_available, which is False without .git; follow-up: give cidx query the git seam |
| `services/git_hook_manager.py` | `GitHookManager.is_git_repository` | `.exists .git` | checks <repo>/.git exists |
| `services/progress_subprocess_runner.py` | `gather_repo_metrics` | `.exists .git` | OPEN: progress-phase weights only (0,0 without .git), no index effect; follow-up: ls_files/rev_list_count via the git backend |
| `services/progress_subprocess_runner.py` | `gather_repo_metrics` | `git ls-files` | OPEN: progress-phase weights only (0,0 without .git), no index effect; follow-up: ls_files/rev_list_count via the git backend |
| `services/progress_subprocess_runner.py` | `gather_repo_metrics` | `git rev-list` | OPEN: progress-phase weights only (0,0 without .git), no index effect; follow-up: ls_files/rev_list_count via the git backend |
| `services/smart_indexer.py` | `SmartIndexer._get_file_commit` | `git log` | OPEN: under a manifest reached only for paths it cannot hash (symlink/unreadable); answers "unknown" -> re-index, never a skip; follow-up |
| `storage/filesystem_vector_store.py` | `FilesystemVectorStore._check_uncommitted_batch` | `git status` | OPEN, fail-safe: only reached when _get_repo_root found a local repo; follow-up with _get_repo_root |
| `storage/filesystem_vector_store.py` | `FilesystemVectorStore._get_blob_hashes_batch` | `git ls-tree` | OPEN, fail-safe: only reached when _get_repo_root found a local repo; follow-up with _get_repo_root |
| `storage/filesystem_vector_store.py` | `FilesystemVectorStore._get_repo_root` | `git rev-parse` | OPEN, fail-safe: no .git on a mountless pod -> None -> chunk_text stored inline, which is what a query there needs; follow-up: decide it from the seam explicitly |
