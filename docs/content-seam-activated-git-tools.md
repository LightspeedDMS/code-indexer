# Content seam: git tools on activated repositories

With `content_backend: delegated` a user's activated working copy is not on
this pod. `GitOperationsService` (the `git_*` MCP tools and REST git routes)
then runs its git through the git seam, addressed by the copy's path. With
`content_backend: local` (the default) every tool runs `run_git_command`
exactly as before.

| What | Under delegated |
|---|---|
| Local subcommands (status, diff, log, add, reset, restore, commit, checkout, branch, merge, stash, clean, rev-parse, show, config --get, remote get-url, ...) | `GitCommandRunner.run` -> git-mcp `git_exec`: git's raw stdout, exit code and stderr, so the service parses exactly what a local run prints |
| Commit attribution (`GIT_AUTHOR_*` / `GIT_COMMITTER_*`) | sent as `git_exec` `identity`; any OTHER git variable a caller sets is refused (never silently dropped) |
| `fetch` / `pull` / `push` | git-mcp `git_fetch` / `git_pull` / `git_push`, with git-mcp's credentials |
| PAT push (`git_push_with_pat`) | refused to leave cidx: the push is git-mcp's `git_push` with git-mcp's credential |
| Conflict markers, `MERGE_HEAD` | working-tree files through the content seam; `rev-parse -q --verify MERGE_HEAD` through git-mcp |
| MCP repository resolution | the registry (this user's activation) and `rev-parse --git-dir` through git-mcp; golden `-global` repos map a pod snapshot to the base clone |

Fail loud: a delegated server whose git backend cannot run the tools (no
`GitCommandRunner`, or `git_backend: local`) refuses with `GitCommandError`;
nothing runs git on this pod.

## Behaviour differences under delegated

* Push credentials and attribution come from git-mcp, not the user's stored
  PAT. git-mcp refuses pushes to `main`/`master` (its protected-branch policy).
* `pushed_commits` / `updated_files` counts that local parsing reads from git's
  network output are 0 (git-mcp returns a summary, not git's transfer output);
  success, failure and conflicts are reported as before.
* Hooks never run in the working copy (`git_exec` disables them).

## Deployment

* git-mcp with `git_exec` (git-mcp MR !21), reachable as the delegated git backend.
* cidx plugin `cidx_git_backend` at or after this change (provides `run`/`push`).
* git-mcp sees the activated-repos root at the same path.
