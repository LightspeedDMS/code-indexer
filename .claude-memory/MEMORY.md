# Memory Index

## Safety Rules (prevent recurring mistakes)
- [feedback_never_reindex_evolution.md](feedback_never_reindex_evolution.md) - NEVER full-re-index evolution (hours + embedder $$); repairs keep worktree bit-identical, temporal OFF, restore from copies
- [feedback_verify_zero_json_chunks_on_indexing.md](feedback_verify_zero_json_chunks_on_indexing.md) - Before chunk-storage/temporal work, verify zero vector_*.json created on all 3 envs — Bug #1528
- [feedback_never_touch_other_repos.md](feedback_never_touch_other_repos.md) - NEVER modify files outside the assigned working directory
- [feedback_admin_password_sacred.md](feedback_admin_password_sacred.md) - NEVER leave admin password changed; restore admin/admin via DB bypass
- [feedback_port_config_locked.md](feedback_port_config_locked.md) - NEVER change port config for cidx-server/HAProxy/firewall — causes 503s
- [feedback_ssh_mcp_only.md](feedback_ssh_mcp_only.md) - NEVER use raw ssh via Bash — MCP SSH tools only
- [feedback_ssh_systemd_restart.md](feedback_ssh_systemd_restart.md) - NEVER kill+nohup for server restarts — systemd only
- [feedback_versioned_path_trap.md](feedback_versioned_path_trap.md) - _resolve_golden_repo_path returns VERSIONED path — never write to it, resolve to base clone first
- [feedback_convert_tool_docs_destructive.md](feedback_convert_tool_docs_destructive.md) - NEVER run tools/convert_tool_docs.py — breaks entire MCP tool surface
- [feedback_no_secrets_in_memory.md](feedback_no_secrets_in_memory.md) - NEVER write secrets/credentials/topology into memory files — they are versioned
- [feedback_never_retry_loop_auth_endpoint.md](feedback_never_retry_loop_auth_endpoint.md) - NEVER retry-loop an auth endpoint — rejection is terminal, repetition locks the account
- [feedback_own_all_repo_changes.md](feedback_own_all_repo_changes.md) - NEVER revert other subagents' changes — own ALL changes found in repo
- [feedback_parallel_agents_shared_tree_no_broad_git_ops.md](feedback_parallel_agents_shared_tree_no_broad_git_ops.md) - N agents editing the same tree: each prompt must forbid git checkout/restore/reset/clean/stash outside its own file list
- [feedback_no_rogue_agents.md](feedback_no_rogue_agents.md) - Never frame unexpected repo state as "rogue/sabotaging agents" — default explanation is user changed it
- [project_own_local_dev_cidx_server.md](project_own_local_dev_cidx_server.md) - I own the local cidx-server.service — track development, no auto-update, keep healthy
- [feedback_sole_developer_own_all_code.md](feedback_sole_developer_own_all_code.md) - I am the SOLE developer across all sessions — never call code/bugs "not ours"
- [feedback_cluster_aware_state_only.md](feedback_cluster_aware_state_only.md) - NEVER use module-level dicts/per-node RAM for cross-request state — PayloadCache or shared DB only
- [feedback_bootstrap_changes_need_installer_and_autoupdater.md](feedback_bootstrap_changes_need_installer_and_autoupdater.md) - Bootstrap/systemd/env/PATH changes need BOTH installer AND auto-updater self-heal
- [feedback_reliability_over_dependency_purity.md](feedback_reliability_over_dependency_purity.md) - Install-footprint purity vs reliability: default to installing the dependency
- [feedback_no_subagent_to_subagent_delegation.md](feedback_no_subagent_to_subagent_delegation.md) - Dispatched subagents must act directly — NEVER spawn nested Task/Agent calls

## Quality Standards
- [feedback_zero_failures_no_excuses.md](feedback_zero_failures_no_excuses.md) - NEVER dismiss test failures as "pre-existing" — zero failures means zero
- [feedback_fix_every_issue_found_no_deferral.md](feedback_fix_every_issue_found_no_deferral.md) - Fix every issue found (even out-of-scope) in the same session — never just file-and-defer
- [feedback_epic_fix_all_bugs_found.md](feedback_epic_fix_all_bugs_found.md) - On a large epic, fix ALL bugs found incl. pre-existing red/flaky tests — clean green suite is the bar
- [feedback_e2e_not_code_inspection.md](feedback_e2e_not_code_inspection.md) - E2E means executing real functionality, NEVER code inspection
- [feedback_e2e_verify_indexes_work.md](feedback_e2e_verify_indexes_work.md) - E2E must verify indexes EXIST on disk and RETURN RESULTS
- [feedback_no_fallbacks_ever.md](feedback_no_fallbacks_ever.md) - NEVER write fallback code paths — one path that works or fails loudly
- [feedback_design_for_900_repo_scale.md](feedback_design_for_900_repo_scale.md) - Design for ~900 repos, not the 30-repo dev server; never sync I/O inside async def
- [feedback_no_settings_to_gate_bug_fixes.md](feedback_no_settings_to_gate_bug_fixes.md) - NEVER add a config setting unless explicitly asked; never gate a bug fix behind one
- [feedback_no_half_wired_features.md](feedback_no_half_wired_features.md) - Never ship a write-path without its read-path (and status-path); verify round-trip end-to-end
- [feedback_no_sleep_in_production.md](feedback_no_sleep_in_production.md) - NEVER add time.sleep() for UI visibility — fix display logic
- [feedback_no_artificial_work_budgets.md](feedback_no_artificial_work_budgets.md) - NEVER cap legitimate work with hardcoded ceilings/turn-caps/timeouts — correctness over bounded cost
- [feedback_storage_backend_dual.md](feedback_storage_backend_dual.md) - NEVER say "SQLite" as if PG doesn't exist — cover both backends
- [feedback_server_e2e_front_door_only.md](feedback_server_e2e_front_door_only.md) - Server E2E MUST use REST/MCP front door, never CLI
- [feedback_prove_root_cause_before_fix.md](feedback_prove_root_cause_before_fix.md) - Prove a stall/concurrency root cause with py-spy dumps BEFORE building a fix
- [feedback_description_refresh_scheduler_requires_staging_validation.md](feedback_description_refresh_scheduler_requires_staging_validation.md) - description_refresh_scheduler.py changes need local AND staging validation — risk of runaway Claude cost
- [feedback_xray_queries_not_in_dashboard.md](feedback_xray_queries_not_in_dashboard.md) - xray_search/xray_search_batch jobs must NOT appear in the dashboard
- [feedback_run_tests_with_timeout_and_monitor.md](feedback_run_tests_with_timeout_and_monitor.md) - NEVER launch tests without --timeout and active monitoring; know expected duration first
- [feedback_faithful_db_mocks.md](feedback_faithful_db_mocks.md) - DB mocks must mirror the real driver (e.g. psycopg3 executemany is on the cursor) — verify writes against real PG
- [feedback_review_local_and_staging_logs_after_testing.md](feedback_review_local_and_staging_logs_after_testing.md) - After testing, audit BOTH local and staging logs; file AND fix any pattern found
- [feedback_holistic_anomaly_scan_every_loop.md](feedback_holistic_anomaly_scan_every_loop.md) - Every loop, scan jobs/logs/health-UI/on-disk artifacts holistically, not just the narrow change
- [feedback_tdd_red_must_be_discriminating.md](feedback_tdd_red_must_be_discriminating.md) - TDD RED must genuinely fail on the buggy code — test the discriminating/boundary input, never the uniform happy case

## Workflow Preferences
- [feedback_autonomous_overnight_file_fix_iterate.md](feedback_autonomous_overnight_file_fix_iterate.md) - Work autonomously; every defect = file + fix + iterate until clean
- [feedback_bug_report_means_report_not_fix.md](feedback_bug_report_means_report_not_fix.md) - "Root cause + bug report" = investigate, file, STOP — no fixing without explicit instruction
- [feedback_always_checkout_development_before_commit.md](feedback_always_checkout_development_before_commit.md) - ALWAYS switch to development before committing — never on master/staging
- [feedback_bump_version_before_staging.md](feedback_bump_version_before_staging.md) - ALWAYS bump version + tag BEFORE promoting to staging
- [feedback_lint_before_commit.md](feedback_lint_before_commit.md) - Run ruff/mypy BEFORE staging — pre-commit hook is a safety net, not primary
- [feedback_no_commit_during_background_agent.md](feedback_no_commit_during_background_agent.md) - NEVER commit a background agent's files while it's still running — verify with git show, not working tree
- [feedback_version_bump_must_be_push_tip.md](feedback_version_bump_must_be_push_tip.md) - The version-bump commit MUST be the tip of its push or CI skips tag creation
- [feedback_check_running_jobs_before_restart.md](feedback_check_running_jobs_before_restart.md) - NEVER restart cidx-server without checking for active long-running jobs
- [feedback_keep_local_server_running.md](feedback_keep_local_server_running.md) - ALWAYS keep the local dev cidx-server running — relaunch if down
- [feedback_ruff_black_version_alignment.md](feedback_ruff_black_version_alignment.md) - Pre-commit ruff version must match system ruff
- [feedback_rest_model_changes_need_fast_automation.md](feedback_rest_model_changes_need_fast_automation.md) - REST/MCP query-model changes need fast-automation.sh too, not just server-fast
- [feedback_no_unnecessary_questions.md](feedback_no_unnecessary_questions.md) - Never stop for obvious next steps — only stop if genuinely blocked
- [feedback_work_agentically_to_staging_no_questions.md](feedback_work_agentically_to_staging_no_questions.md) - During an active mission, don't ask process/workflow questions (commit timing, sequencing) — only stop for irreversible/destructive actions or genuine no-default forks
- [feedback_no_confirmation_on_commands.md](feedback_no_confirmation_on_commands.md) - Direct commands are instructions to execute, not proposals
- [feedback_implement_story_agentic_no_stops.md](feedback_implement_story_agentic_no_stops.md) - /implement-story-spec runs non-stop — no pre-flight questions
- [feedback_progress_reporting_delicate.md](feedback_progress_reporting_delicate.md) - Ask confirmation before ANY changes to progress reporting
- [feedback_targeted_scope_discipline.md](feedback_targeted_scope_discipline.md) - Targeted requests must NOT trigger UI rewrites or unrelated styling changes
- [feedback_use_code_reviewer.md](feedback_use_code_reviewer.md) - Use code-reviewer (opus) for all reviews — Codex credits running low
- [feedback_dual_review_claude_and_codex.md](feedback_dual_review_claude_and_codex.md) - Standing rule: dual review (Claude + independent Codex) for every review gate
- [feedback_opus_arbiter_of_codex_nitpicking.md](feedback_opus_arbiter_of_codex_nitpicking.md) - After 2-3 Codex REJECT rounds on the same fix, dispatch Opus to judge materiality before another round
- [feedback_trust_codex_first_pass.md](feedback_trust_codex_first_pass.md) - When codex flags over-engineering, SIMPLIFY — don't commission counter-reviews
- [feedback_verify_codex_actually_ran.md](feedback_verify_codex_actually_ran.md) - Codex-wrapper agents fall back to Claude silently — verify a real run via ~/.codex/sessions
- [feedback_find_is_bfs_use_mmin.md](feedback_find_is_bfs_use_mmin.md) - `find` here is bfs: relative `-newermt` fails silently — use `-mmin -N`; polls must check exit status
- [project_test_gates_flake_under_load.md](project_test_gates_flake_under_load.md) - fast-automation/server-fast flake under concurrent load — run alone, re-verify in isolation
- [feedback_active_monitoring_check_back.md](feedback_active_monitoring_check_back.md) - Never stay idle on background work — set a check-back timer, detect stalls early
- [feedback_study_anomalies_deeply.md](feedback_study_anomalies_deeply.md) - Root-cause odd behavior with FACTS — never dismiss as "artifact/benign" without evidence
- [feedback_never_stop_never_blame_env.md](feedback_never_stop_never_blame_env.md) - NEVER self-abort or blame environment for slow tests; a stalled subagent is a RETRY not a blocker
- [feedback_agent_stall_detection_needs_reply_not_just_mtime.md](feedback_agent_stall_detection_needs_reply_not_just_mtime.md) - Mtime staleness triggers a PING not a kill; wait for an actual reply before concluding a stall
- [feedback_subagent_phantom_background_wait.md](feedback_subagent_phantom_background_wait.md) - tdd-engineer agents sometimes end their turn believing a phantom monitor will report their own nohup'd run — resume with an explicit synchronous-check instruction

## Architectural Invariants
- [project_eks_is_eventual_deployment_target.md](project_eks_is_eventual_deployment_target.md) - Containerized EKS is the eventual target — no DRBD/Pacemaker/EFS; OntapCloneBackend targets FSx
- [project_verify_both_staging_environments.md](project_verify_both_staging_environments.md) - Every release verification checks BOTH clustered (postgres/HAProxy) AND solo (SQLite) staging
- [project_query_is_everything.md](project_query_is_everything.md) - Query capability is core value — NEVER remove/break query functionality
- [project_reranker_injection_point.md](project_reranker_injection_point.md) - Reranker fires AFTER RRF coalescing, BEFORE truncation
- [project_description_refresh_tracking_split_brain.md](project_description_refresh_tracking_split_brain.md) - FIXED v10.125.0 (#1100): scheduler uses registry tracking backend; validate against PG
- [project_cluster_auto_updater_service.md](project_cluster_auto_updater_service.md) - Auto-updater is a SEPARATE service+timer; cluster installer must provision it + set branch
- [project_nfs_host_down_hangs_systemd.md](project_nfs_host_down_hangs_systemd.md) - CoW/NFS host down hangs daemon-reload + sudo on other nodes; diagnose with timeout-wrapped probes
- [project_cluster_temporal_metadata_pg_backed.md](project_cluster_temporal_metadata_pg_backed.md) - Cluster temporal metadata is PG-backed (#1313); all 5 launch sites need CIDX_TEMPORAL_PG_BOOTSTRAP_DIR
- [project_staging_workers_config_durability.md](project_staging_workers_config_durability.md) - Durable worker-count is the DB runtime.workers setting, set via web-UI form only
- [project_local_server_solo_sqlite.md](project_local_server_solo_sqlite.md) - Local dev cidx-server is solo/SQLite — local E2E validates only that branch; PG/cluster needs staging
- [project_backup_scope_dev_staging_only.md](project_backup_scope_dev_staging_only.md) - Epic #1454 backup-before-migration is dev/staging-only — production has no room for it
- [project_config_default_flip_is_inert.md](project_config_default_flip_is_inert.md) - Changing a dataclass default is INERT on existing deployments — needs a marker-presence promotion
- [project_chunk_storage_write_mode_context.md](project_chunk_storage_write_mode_context.md) - Chunk-storage write mode is context-dependent (server=sqlite, CLI/daemon=json); conversion always explicit
- [project_shadow_mode_not_used_in_production.md](project_shadow_mode_not_used_in_production.md) - query-embedding cache "shadow" mode is NOT what production runs — assume `on` semantics

## External References
- [reference_staging_totp_programmatic_auth.md](reference_staging_totp_programmatic_auth.md) - Headless MFA: `.local-testing` TOTP is a shell command, eval for live code, then two-step login
- [reference_staging_nfs_wedge_recovery.md](reference_staging_nfs_wedge_recovery.md) - Recover wedged cow-storage NFS mount: nfsd per-client force-expire, vers=4.1 pin when 4.2 hangs
- [reference_reranker_api_signatures.md](reference_reranker_api_signatures.md) - Verified Voyage rerank-2.5 and Cohere rerank API params — no native instruction field
- [reference_cow_daemon_architecture.md](reference_cow_daemon_architecture.md) - CoW Storage Daemon: REST API for clone lifecycle, NFS for filesystem access
