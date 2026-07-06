# Memory Index

## Safety Rules (prevent recurring mistakes)
- [feedback_never_touch_other_repos.md](feedback_never_touch_other_repos.md) - NEVER modify files outside the assigned working directory — other clones have their own agents
- [feedback_admin_password_sacred.md](feedback_admin_password_sacred.md) - NEVER leave admin password changed; always restore admin/admin via DB bypass
- [feedback_port_config_locked.md](feedback_port_config_locked.md) - NEVER change port config for cidx-server/HAProxy/firewall — causes 503s
- [feedback_ssh_mcp_only.md](feedback_ssh_mcp_only.md) - NEVER use ssh via Bash — use MCP SSH tools only
- [feedback_ssh_systemd_restart.md](feedback_ssh_systemd_restart.md) - NEVER use kill+nohup for server restarts — use systemd only
- [feedback_versioned_path_trap.md](feedback_versioned_path_trap.md) - _resolve_golden_repo_path returns VERSIONED path — NEVER write to it, resolve to base clone first
- [feedback_convert_tool_docs_destructive.md](feedback_convert_tool_docs_destructive.md) - NEVER run tools/convert_tool_docs.py — silently breaks entire MCP tool surface
- [feedback_no_secrets_in_memory.md](feedback_no_secrets_in_memory.md) - NEVER write IPs, secrets, credentials, topology into memory files — they are versioned
- [feedback_own_all_repo_changes.md](feedback_own_all_repo_changes.md) - NEVER revert other subagents' changes — own ALL changes found in repo
- [feedback_no_rogue_agents.md](feedback_no_rogue_agents.md) - Never frame unexpected repo state as "rogue/sabotaging agents" — default explanation is user changed it
- [feedback_cluster_aware_state_only.md](feedback_cluster_aware_state_only.md) - NEVER use module-level dicts or per-node RAM for cross-request state — use PayloadCache (app.state.payload_cache) or shared DB; HAProxy affinity is not a substitute

## Quality Standards
- [feedback_zero_failures_no_excuses.md](feedback_zero_failures_no_excuses.md) - NEVER dismiss test failures as "pre-existing" — zero failures means zero
- [feedback_e2e_not_code_inspection.md](feedback_e2e_not_code_inspection.md) - E2E means executing real functionality, NEVER code inspection/source checks
- [feedback_e2e_verify_indexes_work.md](feedback_e2e_verify_indexes_work.md) - E2E must verify indexes EXIST on disk and RETURN RESULTS
- [feedback_no_fallbacks_ever.md](feedback_no_fallbacks_ever.md) - NEVER write fallback code paths — one path that works or fails loudly
- [feedback_no_sleep_in_production.md](feedback_no_sleep_in_production.md) - NEVER add time.sleep() for UI visibility — fix display logic
- [feedback_no_artificial_work_budgets.md](feedback_no_artificial_work_budgets.md) - NEVER cap legitimate analysis/indexing work with hardcoded search-call ceilings, agent-turn caps, or per-file/job timeouts — correctness over bounded cost (same disease as Bug #1218); the dep-map "AT MOST 5 search calls" ceiling is a repeat offender
- [feedback_storage_backend_dual.md](feedback_storage_backend_dual.md) - NEVER say "SQLite" as if PG doesn't exist — cover both backends or use agnostic language
- [feedback_server_e2e_front_door_only.md](feedback_server_e2e_front_door_only.md) - Server E2E tests MUST use REST API/MCP front door, never CLI — CLI/SSH only for troubleshooting
- [feedback_prove_root_cause_before_fix.md](feedback_prove_root_cause_before_fix.md) - Prove a stall/concurrency root cause with py-spy thread dumps BEFORE building a fix — don't infer from architecture or conclude "no 429s" from unlogged paths
- [feedback_description_refresh_scheduler_requires_staging_validation.md](feedback_description_refresh_scheduler_requires_staging_validation.md) - description_refresh_scheduler.py changes require local AND staging testing with positive confirmation — mistakes risk runaway Claude processes burning money

- [feedback_xray_queries_not_in_dashboard.md](feedback_xray_queries_not_in_dashboard.md) - xray_search/xray_search_batch jobs must NOT appear in the dashboard — user explicitly requires this

- [feedback_run_tests_with_timeout_and_monitor.md](feedback_run_tests_with_timeout_and_monitor.md) - NEVER launch tests without --timeout and active monitoring; know expected duration before running; fast-automation ≤10min, server-fast ≤15min, unit files ≤30s
- [feedback_faithful_db_mocks.md](feedback_faithful_db_mocks.md) - DB mocks must mirror the real driver; psycopg3 executemany is on the cursor NOT the connection; unfaithful FakeConn certified silent-no-op writes — verify storage writes against real PG
- [feedback_review_local_and_staging_logs_after_testing.md](feedback_review_local_and_staging_logs_after_testing.md) - After testing, ALWAYS audit BOTH local and staging logs; if a pattern points to a bug, file AND fix it (don't just report)

## Workflow Preferences
- [feedback_autonomous_overnight_file_fix_iterate.md](feedback_autonomous_overnight_file_fix_iterate.md) - Work autonomously (no triage questions); every defect found = file + fix + iterate until staging logs are clean; clean logs is the bar
- [feedback_always_checkout_development_before_commit.md](feedback_always_checkout_development_before_commit.md) - ALWAYS switch to development branch before committing — never commit on master/staging
- [feedback_bump_version_before_staging.md](feedback_bump_version_before_staging.md) - ALWAYS bump version + tag BEFORE promoting to staging — auto-deployer requires it
- [feedback_lint_before_commit.md](feedback_lint_before_commit.md) - Run ruff check/format/mypy BEFORE staging — pre-commit hook is safety net, not primary
- [feedback_no_commit_during_background_agent.md](feedback_no_commit_during_background_agent.md) - NEVER git-add/commit a background agent's files while it's still running (add can snapshot a reverse-applied/broken state); verify commit content with git show <sha>:<file>, not just the working tree
- [feedback_version_bump_must_be_push_tip.md](feedback_version_bump_must_be_push_tip.md) - The __init__.py version-bump commit MUST be the tip of its push or CI skips tag creation (compares HEAD~1..HEAD)
- [feedback_check_running_jobs_before_restart.md](feedback_check_running_jobs_before_restart.md) - NEVER restart cidx-server without checking for active long-running jobs
- [feedback_keep_local_server_running.md](feedback_keep_local_server_running.md) - ALWAYS keep the local dev cidx-server (:8000) running — never stop/pkill it; relaunch if down
- [feedback_ruff_black_version_alignment.md](feedback_ruff_black_version_alignment.md) - Pre-commit ruff version must match system ruff; server-fast-automation uses ruff format
- [feedback_rest_model_changes_need_fast_automation.md](feedback_rest_model_changes_need_fast_automation.md) - REST/MCP query-model or query-param changes need fast-automation.sh too (param-parity guard is in the CLI suite), not just server-fast
- [feedback_no_unnecessary_questions.md](feedback_no_unnecessary_questions.md) - Never stop for obvious next steps — only stop if genuinely blocked
- [feedback_no_confirmation_on_commands.md](feedback_no_confirmation_on_commands.md) - Direct commands are instructions to execute, not proposals — never ask "should I proceed?"
- [feedback_implement_story_agentic_no_stops.md](feedback_implement_story_agentic_no_stops.md) - /implement-story-spec runs the FULL workflow non-stop — no pre-flight questions, the story breakdown is already the agreement
- [feedback_progress_reporting_delicate.md](feedback_progress_reporting_delicate.md) - Ask confirmation before ANY changes to progress reporting
- [feedback_targeted_scope_discipline.md](feedback_targeted_scope_discipline.md) - Targeted requests must NOT trigger UI rewrites or unrelated styling changes
- [feedback_use_code_reviewer.md](feedback_use_code_reviewer.md) - Use code-reviewer (opus) for all reviews — Codex credits running low
- [feedback_trust_codex_first_pass.md](feedback_trust_codex_first_pass.md) - When codex flags over-engineering, SIMPLIFY — don't commission counter-reviews
- [feedback_verify_codex_actually_ran.md](feedback_verify_codex_actually_ran.md) - Codex-wrapper agents fall back to Claude silently — verify a real Codex run via ~/.codex/sessions before claiming "codex reviewed it"
- [project_test_gates_flake_under_load.md](project_test_gates_flake_under_load.md) - fast-automation/server-fast flake under concurrent load (SQLite DB-open errors, timeouts); run them ALONE, re-run failures in isolation before concluding regression; omni '*' is MCP-only not REST
- [feedback_active_monitoring_check_back.md](feedback_active_monitoring_check_back.md) - Never stay idle while background agents/jobs run — set a check-back timer and verify progress often; detect stalls early instead of waiting on a completion ping that never comes if it hangs
- [feedback_study_anomalies_deeply.md](feedback_study_anomalies_deeply.md) - When you see odd/anomalous behavior, study it in depth to root cause and prove the classification with FACTS — never dismiss as "artifact/benign/cosmetic" without evidence; "odd" itself is a claim needing facts
- [feedback_never_stop_never_blame_env.md](feedback_never_stop_never_blame_env.md) - NEVER self-abort a mission or blame the environment for slow tests; a stalled subagent is a RETRY not a blocker; do NOT kill a working subagent on a frozen output-file or "no git changes yet" — those are not stall signals

## Architectural Invariants
- [project_query_is_everything.md](project_query_is_everything.md) - Query capability is core value — NEVER remove/break query functionality
- [project_reranker_injection_point.md](project_reranker_injection_point.md) - Reranker fires AFTER RRF coalescing, BEFORE truncation — mandatory pipeline order
- [project_description_refresh_tracking_split_brain.md](project_description_refresh_tracking_split_brain.md) - FIXED v10.125.0 (#1100): scheduler now uses registry tracking backend (PG in cluster mode); validate against PG, not SQLite
- [project_cluster_auto_updater_service.md](project_cluster_auto_updater_service.md) - The auto-updater is a SEPARATE cidx-auto-update.service + timer (not part of cidx-server); the cluster installer must provision it (fixed v11.21.0) and set CIDX_AUTO_UPDATE_BRANCH — staging nodes track staging else default master; retrofit via `cidx server install-auto-update --branch staging`
- [project_nfs_host_down_hangs_systemd.md](project_nfs_host_down_hangs_systemd.md) - When the CoW/NFS host node is down, hard NFS mounts on other nodes hang daemon-reload and cascade into sudo/pam_systemd (every sudo blocks, non-sudo instant); recovers when host returns — diagnose non-sudo with timeout-wrapped probes

## External References
- [reference_reranker_api_signatures.md](reference_reranker_api_signatures.md) - Verified Voyage rerank-2.5 and Cohere rerank API params — no native instruction field
- [reference_cow_daemon_architecture.md](reference_cow_daemon_architecture.md) - CoW Storage Daemon: REST API for clone lifecycle, NFS for filesystem access
