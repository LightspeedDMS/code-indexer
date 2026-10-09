# Forge and Write Tools

The MCP tools that change something: files in a repository, git state, the credentials the server uses to talk to
GitHub and GitLab, server SSH keys, CI runs and pull requests. For each group this guide gives the permission the
tool declares, whether it needs TOTP step-up elevation, and what it changes.

Audience: users and AI agents working through the server's MCP endpoint, and administrators deciding who may use
these tools. Every parameter is in the generated [MCP tool catalog](../reference/mcp-tools/README.md); connecting a
client is covered in [MCP registration](../getting-started/mcp-registration.md).

## Contents

- [Permissions and elevation](#permissions-and-elevation)
- [Where writes land](#where-writes-land)
- [Write mode](#write-mode)
- [File tools](#file-tools)
- [Git write tools](#git-write-tools)
- [Destructive git operations and confirmation tokens](#destructive-git-operations-and-confirmation-tokens)
- [Git credentials](#git-credentials)
- [SSH keys](#ssh-keys)
- [CI tools](#ci-tools)
- [Pull-request tools](#pull-request-tools)
- [Troubleshooting](#troubleshooting)

## Permissions and elevation

Each tool declares one permission in its tool doc (`required_permission` in the frontmatter under
`src/code_indexer/server/mcp/tool_docs/`). Which role holds which permission is listed in
[Accounts and access](../server/auth/accounts-and-access.md#roles).

A few tools also require a TOTP step-up elevation window when the server enforces elevation (the handler carries
`@require_mcp_elevation()`): `configure_git_credential`, `delete_git_credential`, every `manage_ssh_key` action and
`list_ssh_keys`. Without a window they return `{"error": "elevation_required", ...}` (or `totp_setup_required` with
a `setup_url`); call `elevate_session` with a current TOTP code and retry. With enforcement off they run without a
check. Requests authenticated with an MCP credential or a server-issued OAuth access token count as elevated. The
full contract is in [Login and elevation](../server/auth/login-and-elevation.md#step-up-elevation).

## Where writes land

- **Activated repositories.** `activate_repository` gives you your own copy of a golden repository. File and git
  write tools act on that copy, named by its alias in `repository_alias`, and need no write mode.
- **The write-exception repository `cidx-meta-global`.** The server registers this one repository (the
  repository descriptions and dependency maps) for direct editing at startup
  (`register_write_exception` in `src/code_indexer/server/startup/service_init.py`). File tools refuse it until
  write mode is on. It is a local repository, so git tools answer that it `does not support git operations`.
- Golden repositories you have not activated are not a place to edit files: activate them first.

## Write mode

| Tool | Permission | Elevation | Changes |
|------|------------|-----------|---------|
| `enter_write_mode` | `repository:write` | no | takes the repository's write lock and creates a marker file `.write_mode/<alias>.json` in the golden-repos directory |
| `exit_write_mode` | `repository:write` | no | removes the marker, releases the write lock, then runs a refresh of the repository and waits for it |

```json
{"name": "enter_write_mode", "arguments": {"repo_alias": "cidx-meta-global"}}
```

- On success the result carries `alias` and `source_path` (the live directory being edited).
- If another process holds the write lock, the result is `success: false` with a message naming the holder.
- For any repository other than a write-exception one, both tools succeed without doing anything.
- `exit_write_mode` returns only after the refresh has finished, so the next query sees your edits. Called while
  write mode is not active, it succeeds with a `warning`.
- Always call `exit_write_mode`: while the lock is held, the background refresh scheduler cannot refresh that
  repository.

Handlers: `handle_enter_write_mode` / `handle_exit_write_mode` in `src/code_indexer/server/mcp/handlers/files.py`.

## File tools

| Tool | Permission | Elevation | Changes |
|------|------------|-----------|---------|
| `create_file` | `repository:write` | no | creates a new file; fails if it exists |
| `edit_file` | `repository:write` | no | replaces `old_string` with `new_string`; without `replace_all`, `old_string` must occur exactly once (zero or several matches are an error); with `replace_all: true`, every occurrence is replaced |
| `delete_file` | `repository:write` | no | deletes a file |

`file_path` is relative to the repository root. `edit_file` requires `content_hash`, the SHA-256 of the file as you
last saw it: `get_file_content` on an activated repository returns it in its `metadata`, and `create_file` and
`edit_file` return the new value. If the file changed since, the edit is refused and you must read it again. `delete_file` accepts an optional
`content_hash` with the same check (`HashMismatchError` in `src/code_indexer/server/services/file_crud_service.py`).

```json
{"name": "edit_file", "arguments": {"repository_alias": "example-repo", "file_path": "src/app.py",
  "old_string": "def old_name(", "new_string": "def new_name(", "content_hash": "<hash from get_file_content>"}}
```

## Git write tools

All act on one of your activated repositories (`repository_alias`). Handlers:
`src/code_indexer/server/mcp/handlers/git_write.py`.

| Tool | Permission | Elevation | Changes |
|------|------------|-----------|---------|
| `git_stage`, `git_unstage` | `repository:write` | no | adds or removes `file_paths` in the index |
| `git_commit` | `repository:write` | no | commits the staged changes with `message` |
| `git_amend` | `repository:write` | no | rewrites the last commit; keeps its message unless `message` is given |
| `git_push` | `repository:write` | no | pushes to `remote` (default `origin`), `branch` (default current), with upstream tracking by default |
| `git_pull`, `git_fetch` | `repository:write` | no | updates from the remote |
| `git_branch_create`, `git_branch_switch` | `repository:write` | no | creates or checks out a branch |
| `git_checkout_file` | `repository:write` | no | discards local changes to one file |
| `git_merge`, `git_merge_abort`, `git_mark_resolved` | `repository:write` | no | merges `source_branch`, aborts a merge, marks a conflicted file resolved |
| `git_stash` | `repository:write` | no | `action`: `push`, `pop`, `apply`, `list` or `drop` |
| `git_reset` | `repository:admin` | no | `mode`: `soft`, `mixed` (default) or `hard`; `hard` needs a confirmation token |
| `git_clean` | `repository:admin` | no | removes untracked files and directories (`git clean -fd`); needs a confirmation token |
| `git_branch_delete` | `repository:admin` | no | deletes a branch with `git branch -d` (refuses unmerged branches); needs a confirmation token |

Commit identity (`_resolve_commit_identity`): when you have a [git credential](#git-credentials) for the host of
`origin`, author and committer are the name and email discovered from the forge. Otherwise the email is your
account email, or `<username>@cidx.local`, and the author name is `author_name` or your username. There is no
`author_email` parameter.

`git_push` authenticates with your git credential for the host of the chosen remote. Without one it fails with
`No git credential configured for <host>. Use configure_git_credential to set up your PAT.`

## Destructive git operations and confirmation tokens

`git_reset` with `mode: "hard"`, `git_clean` and `git_branch_delete` use a two-call handshake
(`src/code_indexer/server/services/git_confirmation_tokens.py`, gated by `_confirmation_gate` in
`src/code_indexer/server/services/git_operations_service.py`):

1. Call the tool without `confirmation_token` (an empty string counts as none). Nothing changes; the result is:

   ```json
   {"success": false, "confirmation_token_required": {"token": "<token>",
     "message": "Hard reset requires confirmation. Call again with confirmation_token set to the value of the `token` field."}}
   ```

2. Call it again with the same arguments plus `"confirmation_token": "<token>"`. The operation runs.

Token rules:

- a random URL-safe string with 128 bits of randomness (`secrets.token_urlsafe(16)`);
- valid for 5 minutes (`TOKEN_EXPIRY = 300`, measured on the token store's own clock) and for one use;
- bound to the user, the repository alias, the operation and its parameters (the commit for a hard reset, the branch
  for a branch delete), so a `git_clean` token does not confirm a hard reset;
- kept in the shared payload cache (SQLite in solo mode, PostgreSQL in a cluster), so any worker process or cluster
  node can redeem a token another one issued. Only a digest of the token and its binding is stored.

An unknown, expired, reused or differently bound token is refused: the result has the same
`confirmation_token_required` shape with a fresh token, and its message starts with
`Invalid or expired confirmation token`. `git_reset` in `soft` or `mixed` mode needs no token. Over REST, reset and
clean take `confirmation_token` in the JSON body; branch delete takes it in the `X-Confirmation-Token` header (or the
`confirmation_token` query parameter).

## Git credentials

Personal access tokens the server uses on your behalf for `git_push`, pull-request tools, commit identity and CI
write tools. One credential per user, forge type and host; configuring the same host again replaces it
(`ON CONFLICT (username, forge_type, forge_host)` in the git-credentials storage backends).

| Tool | Permission | Elevation | Changes |
|------|------------|-----------|---------|
| `configure_git_credential` | `query_repos` | yes | validates the token against the forge API, discovers your forge username, name and email, stores the token encrypted |
| `list_git_credentials` | `query_repos` | no | nothing; lists your own credentials; the token is shown only as its last 4 characters, and only when it has 20 or more characters (otherwise none of it is shown) |
| `delete_git_credential` | `query_repos` | yes | deletes one credential by `credential_id` |

```json
{"name": "configure_git_credential", "arguments": {"forge_type": "github", "forge_host": "github.com",
  "token": "<personal access token>", "name": "Work GitHub"}}
```

`forge_type` is `github` or `gitlab`; `forge_host` may be a self-hosted instance. Configuring and deleting a
credential are written to the audit log. The Web UI offers the same management for your own account.

## SSH keys

Server SSH keys, used by the server's own git access over SSH (for example cloning private repositories). They
belong to the server, not to one user. Handlers: `src/code_indexer/server/mcp/handlers/ssh_keys.py`.

| Tool | Permission | Elevation | Changes |
|------|------------|-----------|---------|
| `manage_ssh_key` `create` | `repository:admin` | yes | generates a key pair (`key_type` `ed25519` default, or `rsa`) and returns the public key |
| `manage_ssh_key` `delete` | `repository:admin` | yes | removes a managed key and its host mappings |
| `manage_ssh_key` `show_public` | `repository:admin` | yes | nothing; returns the public key |
| `manage_ssh_key` `assign_host` | `repository:admin` | yes | adds a `Host` entry for `hostname` to the server's SSH configuration (`force` overwrites a conflicting entry) |
| `list_ssh_keys` | `repository:admin` | yes | nothing; lists keys and their hosts |

Every action also requires the `admin` role in the handler. The same operations exist under REST `/api/ssh-keys`
and in the remote CLI as `cidx keys` ([reference](../reference/cli/keys.md#cidx-keys)).

## CI tools

GitHub Actions and GitLab CI for a golden repository, named by its alias (for example `example-repo-global`). The
forge is detected from the host of the repository's remote URL: a host name containing `github` or `gitlab`
(`forge_client.py`). For any other host the tool answers `Could not auto-detect forge from repository remote URL...`;
pass `forge: "github"` or `forge: "gitlab"`. Handlers: `src/code_indexer/server/mcp/handlers/cicd.py`.

| Tool | Permission | Elevation | Changes |
|------|------------|-----------|---------|
| `ci_list_runs` | `repository:read` | no | nothing; recent runs, filterable by `branch`, `status`, `limit` |
| `ci_get_run`, `ci_get_job_logs`, `ci_search_logs` | `repository:read` | no | nothing; run details, a job's full log, a pattern search in a run's logs |
| `ci_retry_run` | `repository:write` | no | re-runs a failed run on the forge |
| `ci_cancel_run` | `repository:write` | no | cancels a running or queued run on the forge |

Tokens (`_resolve_cicd_read_token` / `_resolve_cicd_write_token`): read tools on a repository registered on the
server may use the server's shared CI token configured by the operator, and fall back to your own git credential for
the forge host. `ci_retry_run` and `ci_cancel_run` always use your own git credential; without one they fail with a
message asking you to configure it. The remote CLI equivalent is `cidx cicd`
([reference](../reference/cli/cicd.md#cidx-cicd)).

## Pull-request tools

GitHub pull requests and GitLab merge requests for an activated repository. The forge is detected from the
`origin` URL, and the token is your git credential for that host. Handlers:
`src/code_indexer/server/mcp/handlers/pull_requests.py`.

| Tool | Permission | Elevation | Changes |
|------|------------|-----------|---------|
| `create_pull_request` | `repository:write` | no | opens a PR/MR from `head` into `base` |
| `merge_pull_request` | `repository:write` | no | merges with `merge_method` `merge` (default), `squash` or `rebase`; `delete_branch` removes the source branch |
| `close_pull_request` | `repository:write` | no | closes without merging |
| `update_pull_request` | `query_repos` | no | changes title, description, labels, assignees or reviewers |
| `comment_on_pull_request` | `query_repos` | no | adds a comment, optionally on `file_path` and `line_number` |
| `list_pull_requests`, `get_pull_request`, `list_pull_request_comments` | `query_repos` | no | nothing |

`create_pull_request` requires a writable repository (an activated one, or write mode) and accepts an optional
`token` that overrides the stored credential. A typical sequence on an activated repository:

```
git_branch_create -> git_branch_switch -> edit_file -> git_stage -> git_commit -> git_push -> create_pull_request
```

## Troubleshooting

| Result | Meaning and fix |
|--------|-----------------|
| `Repo 'cidx-meta-global' requires write mode. Call enter_write_mode(...)` | Call `enter_write_mode`, edit, then `exit_write_mode`. |
| `Write lock for '<alias>' is already held by '<owner>'` | Another writer or a refresh holds the lock; retry when it finishes. |
| `Repository '<alias>' is not an activated workspace for user ...` | Activate the repository first, or check the alias. |
| `edit_file` refused with a hash mismatch | The file changed since you read it; call `get_file_content` again and use the new `content_hash`. |
| `confirmation_token_required` | Expected on the first call of a destructive operation; repeat the call with the returned token. |
| `Invalid or expired confirmation token` | Use the fresh token in the same result, within 5 minutes. |
| `No git credential configured for <host>` | Run `configure_git_credential` for that host. |
| `elevation_required` | Call `elevate_session` with a current TOTP code, then retry. |
| `Unable to determine forge host from remote ...` (`git_push`) | The remote has no URL the server can parse; check `git remote -v` in the repository. |
| `Could not auto-detect forge from repository remote URL...` (CI tools) | The remote host name contains neither `github` nor `gitlab`; pass `forge` explicitly. |
