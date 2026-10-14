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
| `fetch` / `pull` / `push` | git-mcp `git_fetch` / `git_pull` / `git_push`, with git-mcp's credentials; `--set-upstream` is push's only option that crosses, any other option is refused |
| Pull conflicts | git-mcp returns `{status: 'conflicts'}`; the plugin raises it as a failed pull whose output carries git's `CONFLICT (content): Merge conflict in <path>` lines, so `git_pull` reports `success: false` and the conflicted paths as locally |
| PAT push (`git_push_with_pat`) | refused to leave cidx: the push is git-mcp's `git_push` with git-mcp's credential; upstream tracking is set only when `set_upstream` asks (git-mcp's own default is to set it) |
| Conflict markers, `MERGE_HEAD` | working-tree files through the content seam; `rev-parse -q --verify MERGE_HEAD` through git-mcp |
| MCP repository resolution | the registry (this user's activation) and `rev-parse --git-dir` through git-mcp; golden `-global` repos map a pod snapshot to the base clone |

Fail loud: a delegated server whose git backend cannot run the tools (no
`GitCommandRunner`, or `git_backend: local`) refuses with `GitCommandError`;
nothing runs git on this pod.

## Behaviour differences under delegated

* Push credentials and attribution come from git-mcp, not the user's stored
  PAT. git-mcp refuses pushes to `main`/`master` (its protected-branch policy).
* `pushed_commits` is 0 (git-mcp returns a summary, not git's transfer
  output). A pull's `updated_files` is read from the copy (`git diff
  --shortstat` from the pre-pull HEAD), so it matches a local pull; success,
  failure and conflicts are reported as before.
* Hooks never run in the working copy (`git_exec` disables them).

## Deployment

* git-mcp with `git_exec` (git-mcp MR !21), reachable as the delegated git backend.
* cidx plugin `cidx_git_backend` at or after this change (provides `run`/`push`, raises pull conflicts).
* git-mcp sees the activated-repos root at the same path.
