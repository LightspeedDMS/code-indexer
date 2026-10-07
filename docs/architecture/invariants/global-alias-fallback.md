# Global Repository Alias Fallback

Index of all invariant groups: [README](README.md).

Read-only MCP handlers promote a bare alias (for example `example-repo`) to its global form (`example-repo-global`)
when all of these hold:

1. the alias does not already end with `-global`;
2. the user has not activated a repository under that alias
   (`ActivatedRepoManager.user_has_activated_repo(username, alias)`);
3. the golden repository is globally active (`GoldenRepoManager.is_globally_active(alias)`).

The helper is `try_global_fallback(alias, golden_repo_manager)` in
`src/code_indexer/server/mcp/handlers/_global_fallback.py`. It is a pre-check before resolution, not a
catch-and-retry, and an activated repository always takes precedence.

Modules that use it (read paths only): `handlers/search/code_search.py`, `handlers/search/regex_search.py`,
`handlers/files.py`, `handlers/git_read.py`, `handlers/scip.py`, `handlers/repos.py` (branch listing),
`handlers/xray/_search.py`, `handlers/xray/_explore.py`, `handlers/xray/_dump_ast.py` and `handlers/xray_batch.py`.

Invariant: write and mutation handlers stay strict. `_global_fallback.py` is never imported from file create, edit or
delete handlers, git write handlers, pull-request or CI handlers, provider-index, reindex or health handlers, or the
shared path resolvers (`_resolve_git_repo_path`, `_resolve_repo_path`, `_get_repository_path`).
