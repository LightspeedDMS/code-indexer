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

## Quality Standards
- [feedback_zero_failures_no_excuses.md](feedback_zero_failures_no_excuses.md) - NEVER dismiss test failures as "pre-existing" — zero failures means zero
- [feedback_e2e_not_code_inspection.md](feedback_e2e_not_code_inspection.md) - E2E means executing real functionality, NEVER code inspection/source checks
- [feedback_e2e_verify_indexes_work.md](feedback_e2e_verify_indexes_work.md) - E2E must verify indexes EXIST on disk and RETURN RESULTS
- [feedback_no_fallbacks_ever.md](feedback_no_fallbacks_ever.md) - NEVER write fallback code paths — one path that works or fails loudly
- [feedback_no_sleep_in_production.md](feedback_no_sleep_in_production.md) - NEVER add time.sleep() for UI visibility — fix display logic
- [feedback_storage_backend_dual.md](feedback_storage_backend_dual.md) - NEVER say "SQLite" as if PG doesn't exist — cover both backends or use agnostic language
- [feedback_server_e2e_front_door_only.md](feedback_server_e2e_front_door_only.md) - Server E2E tests MUST use REST API/MCP front door, never CLI — CLI/SSH only for troubleshooting
- [feedback_prove_root_cause_before_fix.md](feedback_prove_root_cause_before_fix.md) - Prove a stall/concurrency root cause with py-spy thread dumps BEFORE building a fix — don't infer from architecture or conclude "no 429s" from unlogged paths

## Workflow Preferences
- [feedback_always_checkout_development_before_commit.md](feedback_always_checkout_development_before_commit.md) - ALWAYS switch to development branch before committing — never commit on master/staging
- [feedback_bump_version_before_staging.md](feedback_bump_version_before_staging.md) - ALWAYS bump version + tag BEFORE promoting to staging — auto-deployer requires it
- [feedback_lint_before_commit.md](feedback_lint_before_commit.md) - Run ruff check/format/mypy BEFORE staging — pre-commit hook is safety net, not primary
- [feedback_check_running_jobs_before_restart.md](feedback_check_running_jobs_before_restart.md) - NEVER restart cidx-server without checking for active long-running jobs
- [feedback_ruff_black_version_alignment.md](feedback_ruff_black_version_alignment.md) - Pre-commit ruff version must match system ruff; server-fast-automation uses ruff format
- [feedback_no_unnecessary_questions.md](feedback_no_unnecessary_questions.md) - Never stop for obvious next steps — only stop if genuinely blocked
- [feedback_no_confirmation_on_commands.md](feedback_no_confirmation_on_commands.md) - Direct commands are instructions to execute, not proposals — never ask "should I proceed?"
- [feedback_implement_story_agentic_no_stops.md](feedback_implement_story_agentic_no_stops.md) - /implement-story-spec runs the FULL workflow non-stop — no pre-flight questions, the story breakdown is already the agreement
- [feedback_progress_reporting_delicate.md](feedback_progress_reporting_delicate.md) - Ask confirmation before ANY changes to progress reporting
- [feedback_targeted_scope_discipline.md](feedback_targeted_scope_discipline.md) - Targeted requests must NOT trigger UI rewrites or unrelated styling changes
- [feedback_use_code_reviewer.md](feedback_use_code_reviewer.md) - Use code-reviewer (opus) for all reviews — Codex credits running low
- [feedback_trust_codex_first_pass.md](feedback_trust_codex_first_pass.md) - When codex flags over-engineering, SIMPLIFY — don't commission counter-reviews
- [feedback_verify_codex_actually_ran.md](feedback_verify_codex_actually_ran.md) - Codex-wrapper agents fall back to Claude silently — verify a real Codex run via ~/.codex/sessions before claiming "codex reviewed it"

## Architectural Invariants
- [project_query_is_everything.md](project_query_is_everything.md) - Query capability is core value — NEVER remove/break query functionality
- [project_reranker_injection_point.md](project_reranker_injection_point.md) - Reranker fires AFTER RRF coalescing, BEFORE truncation — mandatory pipeline order

## External References
- [reference_reranker_api_signatures.md](reference_reranker_api_signatures.md) - Verified Voyage rerank-2.5 and Cohere rerank API params — no native instruction field
- [reference_cow_daemon_architecture.md](reference_cow_daemon_architecture.md) - CoW Storage Daemon: REST API for clone lifecycle, NFS for filesystem access
