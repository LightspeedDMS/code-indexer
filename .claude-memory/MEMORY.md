# Memory Index

- [feedback_never_touch_other_repos.md](feedback_never_touch_other_repos.md) - NEVER modify files outside ~/Dev/code-indexer-master/, especially ~/Dev/code-indexer/ where another agent works
- [feedback_zero_failures_no_excuses.md](feedback_zero_failures_no_excuses.md) - NEVER dismiss test failures as "pre-existing" — zero failures means zero, fix them all
- [feedback_e2e_not_code_inspection.md](feedback_e2e_not_code_inspection.md) - E2E means executing real functionality, NEVER code inspection/source checks
- [project_bug469_472_package.md](project_bug469_472_package.md) - Bug package #469-472: implement all, deploy to staging only, E2E regression test
- [reference_cidx_cluster_topology.md](reference_cidx_cluster_topology.md) - CIDX cluster test topology — staging is STANDALONE, NEVER cluster. See .local-testing for IPs
- [feedback_ruff_black_version_alignment.md](feedback_ruff_black_version_alignment.md) - Pre-commit ruff version must match system ruff; server-fast-automation uses ruff format (not black)
- [feedback_admin_password_sacred.md](feedback_admin_password_sacred.md) - NEVER leave admin password changed; always restore admin/admin via DB bypass
- [feedback_versioned_path_trap.md](feedback_versioned_path_trap.md) - _resolve_golden_repo_path returns VERSIONED path — NEVER write to it, resolve to base clone first
- [feedback_e2e_verify_indexes_work.md](feedback_e2e_verify_indexes_work.md) - E2E must verify indexes EXIST on disk and RETURN RESULTS — not just that code runs
- [feedback_ssh_mcp_only.md](feedback_ssh_mcp_only.md) - NEVER use ssh via Bash — use MCP SSH tools only
- [feedback_ssh_systemd_restart.md](feedback_ssh_systemd_restart.md) - NEVER use kill+nohup for server restarts — use systemd only
- [feedback_port_config_locked.md](feedback_port_config_locked.md) - NEVER change port config for cidx-server/HAProxy/firewall — causes 503s
- [feedback_no_sleep_in_production.md](feedback_no_sleep_in_production.md) - NEVER add time.sleep() for UI visibility — fix display logic
- [feedback_progress_reporting_delicate.md](feedback_progress_reporting_delicate.md) - Ask confirmation before ANY changes to progress reporting
- [project_query_is_everything.md](project_query_is_everything.md) - Query capability is core value — NEVER remove/break query functionality
- [feedback_use_codex_for_reviews.md](feedback_use_codex_for_reviews.md) - Always use codex-code-reviewer (not code-reviewer) for all code reviews in this project
- [reference_reranker_api_signatures.md](reference_reranker_api_signatures.md) - Verified Voyage rerank-2.5 and Cohere rerank API params — no native instruction field in either; Voyage prepends instruction to query, Cohere concatenates
- [project_reranker_injection_point.md](project_reranker_injection_point.md) - Reranker fires AFTER dual-provider RRF coalescing, BEFORE truncation/caching — mandatory pipeline order for Story #653
