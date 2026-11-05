# Content seam: golden git history and repository analysis

With `content_backend: delegated` a golden repo's directory on this pod holds
index state only; the repository is with workspace-mcp/git-mcp. With
`content_backend: local` (the default) everything below runs exactly as before.

## Golden git history tools

| Tool | Under delegated |
|---|---|
| `git_log` (unfiltered / `since`), `git_show_commit` metadata, `git_file_at_revision`, branch listing | git-mcp typed tools (`git_log_raw`, `git_rev_parse`, `git_show_objects`, `git_for_each_ref`) |
| `git_blame`, `git_file_history`, `git_search_commits`, `git_search_diffs`, `git_diff` (MCP tool and `get_diff`), `git_show_commit` stats/diff, `git_log` with path/author/until filters (both handlers) | the SAME argv as local, through git-mcp `git_exec` |

`git_exec` returns git's raw stdout; the cidx plugin decodes it exactly as a
local `run_git_command` (locale encoding, strict, universal newlines), so the
existing parsers are unchanged and the output is byte-identical (pinned by
`tests/unit/server/services/test_golden_git_history_git_exec.py`).

**Read-only.** Golden repositories are never modified. `GoldenGitReader`
(`server/services/git_tools_seam.py`) checks every argv with
`require_read_only_git` before any request: an allowlist of subcommands
(`log show diff blame rev-parse rev-list`) and, per subcommand, the whole
options the history tools build. A denylist cannot work: git accepts
unambiguous abbreviations of long options (`blame --cont=<file>` is
`--contents`).

**Fail loud.** A git-mcp failure (transport, refusal) raises
`GitToolsUnavailableError`, never a `CalledProcessError` a tool would read as
"no commits" / "not found". A git-mcp timeout is the local `TimeoutExpired`.
A git backend plugin without `run` (no `git_exec`) refuses.

**Access first.** `_resolve_git_repo_path(..., via_git_backend=True)` checks
group access before any backend call; other git tools on a golden repo
(conflict status, PR tools, write tools) still refuse under delegated.

## Lifecycle / description analysis

The analysis is dispatched with `cwd` = the golden repo's pod path. Under
delegated it runs when the configured CLI invoker provisions the repository
itself (`invoker_resolves_golden_repositories`: the plugin module sets
`RESOLVES_GOLDEN_REPOSITORIES = True`):

* `cidx_orchestrator_invoker` resolves the repo with
  `content_backend_wiring.golden_repo_source`: the golden registry (registered
  remote, credentials stripped, and branch), then the commit cidx indexed from git-mcp
  (`rev_parse HEAD` at the base clone). The job carries `repoUrl`/`branch`,
  `metadata.commit` and a `systemPrompt` pinning the agent to that commit.
* A registry or git-mcp failure, or a `local://` repo, fails the job; the
  agent is never handed "no repository".
* The description scheduler's backfills/periodic refresh and the refresh
  reconcile's description queueing start under the same condition.
* With the built-in local Claude CLI (it reads the pod path) the analysis and
  `RepoAnalyzer` still refuse under delegated.
* The add hook (`meta_description_hook.on_repo_added`) never runs the local
  CLI while a CLI invoker plugin is configured: the description is written by
  the lifecycle path through the plugin, gated by
  `indexing_config.golden_description_generation_enabled` (EVO-64616). With no
  plugin the hook is unchanged.
* `content_backend: delegated` with no CLI invoker plugin refuses to start
  (`require_invoker_for_delegated_content`, called from `service_init`), naming
  `CIDX_CLI_INVOKER` and the `code_indexer.cli_invoker` entry point -- one
  startup failure instead of one ERROR per golden repo add. The check is
  skipped while `golden_description_generation_enabled` is false at startup,
  so that opt-out still starts a plugin-less delegated pod; turning generation
  back on at runtime without a plugin brings back the per-add errors until
  the next restart refuses.

## Empty repositories (unborn HEAD)

A golden repo registered from an empty remote has no commit, so git answers
every HEAD probe with `ambiguous argument 'HEAD'`. `git_head.is_unborn_head`
recognises exactly that answer (a `CalledProcessError` whose stderr carries
it; a transport failure never matches) and the HEAD probers treat it as "no
commit", as their local paths already do: index freshness (cached as git's
definitive answer, logged at DEBUG), `gather_repo_metrics` (0 commits, local
and delegated), the GAP-4 reconcile (no HEAD to compare) and default-branch
detection after a clone (`main`, logged at INFO). Every other failure keeps
its WARNING. Guard: `tests/unit/services/test_unborn_head_is_empty_evo64616.py`.

The same answer is a skip for analysis and SCIP:
`content_backend_wiring.golden_repo_source` raises `GoldenRepoEmptyError` (a
`GoldenRepoSourceError`, so no unaware caller hands an agent "no
repository"); the lifecycle invoker resolves the source before submitting a
job, and `LifecycleBatchRunner` skips the repo at INFO without counting a
failure. SCIP's `head_commit` answers None and `repo_has_root_build_file`
False. Guard: `tests/unit/server/services/test_empty_repo_source_and_scip_evo64616.py`.

## git-mcp `git_exec` hardening (EVO-64616)

Before the hardening in git-mcp !21 (2ea6c74), `git_exec`'s forbidden-option
denylist could be bypassed: `blame --cont=<file>` (abbreviated `--contents`) printed an
arbitrary pod file; `--pathspec-from-f=`, `commit --fil=`, `config --fil=`
read one; `blame -S <file>` / `--ignore-revs-file` leaked its first line in
the error. `git_exec` now refuses abbreviations of forbidden options and the
file-reading short forms. cidx's own allowlist does not depend on it.

## Deployment

* git-mcp with `git_exec` (git-mcp !21, including its option hardening).
* cidx plugins `cidx_git_backend` (`run` decodes like a local run) and
  `cidx_orchestrator_invoker` at or after this change.
