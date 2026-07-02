# cidx-meta backup contract (Story #926)

This document captures the cidx-meta backup contract invariants extracted from the project CLAUDE.md to keep that file focused on rules and rituals.

The server can maintain a continuous git backup of the cidx-meta directory to a remote repository. Key invariants:

**Mutable base path only**: All git operations (bootstrap, sync, rebase, push) execute against `<server_data_dir>/data/golden-repos/cidx-meta/`. NEVER operate inside `.versioned/cidx-meta/v_{timestamp}/` snapshot directories. Use `get_cidx_meta_path(server_data_dir)` from `src/code_indexer/server/services/cidx_meta_backup/paths.py` — single source of truth for both the route and the refresh scheduler.

**Index always runs after sync**: `CidxMetaBackupSync.sync()` runs BEFORE indexing in the refresh path. If sync succeeds (or partially fails with push-only error), indexing still runs. This is the deferred-failure pattern — a push failure becomes a `sync_failure` on the `SyncResult`, which causes the job to be marked FAILED after indexing completes.

**Push/fetch failure is deferred, conflict failure is immediate**: Network errors (fetch fail, push fail) are captured in `SyncResult.sync_failure` and surfaced as `RuntimeError` at the end of the refresh job. Conflict resolution failure raises `RuntimeError` immediately (after `git rebase --abort`) and short-circuits indexing.

**URL-change idempotency**: Changing the remote URL in the Web UI triggers `CidxMetaBackupBootstrap.bootstrap()` at Save time. The refresh scheduler also calls bootstrap at the start of every backup-enabled refresh cycle (idempotent — reads `git remote get-url origin`, no-ops on match). URL changes applied via direct DB edits are thus applied on the next refresh without requiring a Save.

**Externalized conflict-resolution prompt**: `src/code_indexer/server/mcp/prompts/cidx_meta_conflict_resolution.md` — editable by operators. Must contain `{conflict_files}`, `{branch}`, and `{repo_path}` format placeholders.

**Claude CLI routing**: Conflict resolution invokes Claude via `invoke_claude_cli()` in `src/code_indexer/global_repos/repo_analyzer.py` (Story #885 A10 boundary). On 600 s timeout, SIGTERM is sent first; SIGKILL follows after `_CLAUDE_TERMINATION_GRACE_PERIOD_SECONDS` (30 s).

**Branch detection**: `detect_default_branch(master_path)` from `src/code_indexer/server/services/cidx_meta_backup/branch_detect.py` is called at the start of each backup sync to support remotes with `main` as default. Falls back to `"master"` when detection fails.

Files: `src/code_indexer/server/services/cidx_meta_backup/` (bootstrap, sync, conflict_resolver, branch_detect, paths), `src/code_indexer/global_repos/refresh_scheduler.py` (backup branch), `src/code_indexer/server/web/routes.py` (config save route).
