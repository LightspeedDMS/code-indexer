# Remote CLI

How to use the `cidx` command line against a CIDX server (remote mode): connecting a project directory, which
commands run on the server and which stay local, the everyday workflows, and how administrative commands handle
TOTP step-up elevation.

Audience: developers and administrators who already have an account on a CIDX server. Every option of every
command is listed in the generated [CLI reference](../reference/cli/README.md); this guide explains how the commands
fit together. Running a server is covered in [Server deployment](../server/deployment.md).

## Contents

- [Prerequisites](#prerequisites)
- [Connecting a project](#connecting-a-project)
- [Credentials and tokens](#credentials-and-tokens)
- [What runs where](#what-runs-where)
- [Everyday workflow](#everyday-workflow)
- [Querying in remote mode](#querying-in-remote-mode)
- [Files and git](#files-and-git)
- [Administration and elevation](#administration-and-elevation)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- The server URL (for example `https://cidx.example.com`) and an account on it. Roles decide what you may do: see
  [Accounts and access](../server/auth/accounts-and-access.md#roles).
- For `cidx query` from a working copy: a git clone whose `origin` remote is a repository registered on the server.
  The other remote commands take a repository alias and work from any connected directory.

## Connecting a project

```bash
cd ~/src/example-repo
cidx init --remote https://cidx.example.com --username alice --password 'your-password'
```

`--username` and `--password` are both required with `--remote`; the command does not prompt for them. It then
(`initialize_remote_mode` in `src/code_indexer/remote/initialization.py`):

1. validates and normalises the server URL,
2. checks that the server is reachable,
3. logs in with the credentials,
4. writes `.code-indexer/.remote-config` (server URL, username, encrypted credentials),
5. stores the encrypted password in `.code-indexer/.creds`.

`.creds` and `.remote-config` are written with file mode `0600`.

If any step fails, the partial `.remote-config` and `.creds` files are removed and the command exits with status 1.
The success message suggests `cidx start` as a next step; that command is local-only and is not needed in remote
mode.

Every `cidx` command finds its configuration by walking up from the current directory (at most 10 levels) to the
first `.code-indexer/` directory. A `.remote-config` file with a non-empty `server_url` (starting with `http://` or
`https://`) and non-empty `encrypted_credentials` makes that directory remote, even if a local `config.json` exists
next to it (`CommandModeDetector` in `src/code_indexer/mode_detection/command_mode_detector.py`).

The password goes on the command line, so it can land in your shell history. Keep `.code-indexer/` out of version
control.

## Credentials and tokens

The CLI keeps your password (encrypted) rather than only a session token. Each request carries a bearer token from
`POST /auth/login`; when the token is expired, close to expiry or missing, the client logs in again with the stored
password (`_get_valid_token` in `src/code_indexer/api_clients/base_client.py`). Server tokens are short-lived (see
[REST API](../reference/rest-api.md#authentication)). For an account without TOTP MFA this renewal needs no action.
For an account with TOTP MFA, only `cidx auth login` asks for a code: when another command needs a new token it
fails with `This account requires TOTP MFA to log in. ...`, and you run `cidx auth login` again, then rerun the
command.

| Command | Effect |
|---------|--------|
| `cidx auth status` | show the stored login (`-v` for detail, `--health` to check the stored credentials) |
| `cidx auth validate` | check the stored credentials against the server, silently unless `-v` |
| `cidx auth login` | log in, prompting for username and password when they are not given |
| `cidx auth update --username U --password P` | replace the stored credentials, keeping the repository link |
| `cidx auth refresh` | refresh the token now |
| `cidx auth change-password` | change your own password on the server |
| `cidx auth logout` | log out and clear the stored credentials |

When your account has TOTP MFA enabled, `cidx auth login` asks for a TOTP code after the password. The prompt
appears only in an interactive terminal; without a terminal (scripts, CI) the login fails with an error instead of
waiting for input. Options: [auth reference](../reference/cli/auth.md#cidx-auth).

## What runs where

The mode check is the `@require_mode(...)` decorator on each command (`src/code_indexer/disabled_commands.py`); a
command used in the wrong mode exits with `Command '<name>' is not available in ...`. The table below lists what that
check enforces, plus the commands that carry no mode check. The mode markers in `cidx --help` come from a separate static table (`COMMAND_COMPATIBILITY`) and are
informational only; where they differ, the table below is what the CLI does.

| Group | Commands |
|-------|----------|
| Remote only (sent to the server) | `auth`, `admin`, `repos`, `jobs`, `system`, `files`, `git`, `cicd`, `keys`, `remote-index`, `sync` |
| Remote and local (behaviour adapts to the mode) | `query`, `status`, `scip`, `clean`, `list-collections`, `uninstall`, `fix-config`; also `init`, `teach-ai` and `help`, which carry no mode check |
| Local only (refused in remote mode) | `index`, `watch`, `watch-stop`, `start`, `stop` |
| Not mode-checked; act on this machine, not on the server | `config`, `clean-data` (local project data), `global`, `set-global-refresh`, `show-global` (local golden-repo registry), `ssh-key` (keys in your local `~/.ssh`), `xray` (local tree-sitter search), `server` (manages a server installed on this machine), `install-server`, `health` |

Notes:

- `server add-index` and `server list-indexes` are the exception inside `server`: they read the remote
  configuration of the current directory and call the server's admin API.
- `cidx keys` (remote) manages SSH keys stored on the server; `cidx ssh-key` (local) manages keys in your own
  `~/.ssh`.
- The top-level `groups` and `credentials` groups have no subcommands. Use `cidx admin groups` and
  `cidx admin mcp-credentials`.
- Outside a remote-mode directory, `--help` on a subcommand of a remote-only group (for example
  `cidx files create --help`) fails with the mode error, because the group's mode check runs before the subcommand
  parses `--help`. `cidx files --help` works anywhere, and the [CLI reference](../reference/cli/README.md) shows every
  subcommand.

## Everyday workflow

```bash
cidx auth status                          # which server and user this directory uses
cidx repos available                      # golden repositories you can activate
cidx repos activate example-repo          # your own activated copy (background job)
cidx repos activate example-repo --as example-feature --branch feature-x
cidx repos list                           # your activated repositories
cidx jobs list --status running           # background jobs (default: 10 most recent)
cidx jobs status <job-id>                 # one job in detail
cidx jobs cancel <job-id>
cidx repos sync example-repo              # pull and re-index an activated repository
cidx system health                        # server health (-d for components)
```

`cidx repos activate` calls `POST /api/repos/activate`, which accepts `power_user` and `admin` accounts
(`get_current_power_user` in `src/code_indexer/server/routers/inline_repos.py`). References:
[repos](../reference/cli/repos.md#cidx-repos), [jobs](../reference/cli/jobs.md#cidx-jobs),
[system](../reference/cli/system.md#cidx-system), [sync](../reference/cli/commands.md#cidx-sync).

## Querying in remote mode

```bash
cidx query "token refresh logic" --limit 5
cidx query "token refresh logic" --repos example-repo-global,other-repo-global
```

- **Repository link.** On the first `cidx query` the CLI reads `git config remote.origin.url` and the current
  branch, asks the server for repositories registered from that URL, and links the directory to a repository whose
  branch matches, preferring one of your activated repositories. If only a golden repository matches, the CLI tries
  to activate it for you. The link is saved in `.code-indexer/.remote-config` and reused by later queries
  (`src/code_indexer/remote/repository_linking.py`).
- **What is sent.** A single-repository remote query is a semantic search. It sends the query text, `--limit`
  (1 to 100, default 10), `--min-score`, the first `--language` and the first `--path-filter`
  (`execute_remote_query` in `src/code_indexer/remote/query_execution.py`). FTS, regex, hybrid and temporal options,
  `--exclude-language`, `--exclude-path`, `--file-extensions`, `--accuracy` and the rerank options are not sent.
- **Several repositories.** `--repos a,b` queries several server repositories in one call
  (`POST /api/query/multi`); it is available in remote mode only.
- **`--repo` is local.** `--repo ALIAS` resolves a global alias from the local golden-repos directory, not from the
  server; see [Querying other repositories](query.md#querying-other-repositories).

The full set of search modes and parameters, including the server-side REST and MCP equivalents, is in the
[Query guide](query.md).

## Files and git

`cidx files` and `cidx git` act on one of your activated repositories on the server, named with `-r/--repository`.

```bash
cidx files create notes/todo.md -r example-repo --content "# To do"
cidx files edit src/app.py -r example-repo --old "old_name" --new "new_name"
cidx git status -r example-repo
cidx git stage -r example-repo src/app.py
cidx git commit -r example-repo -m "Rename helper"
cidx git push -r example-repo
```

`cidx git push` calls the REST push endpoint (`push_to_remote` in
`src/code_indexer/server/services/git_operations_service.py`). REST and the MCP `git_push` tool share one push
implementation (`src/code_indexer/server/git/git_push.py`); they differ in the credential. REST supplies the
credentials of the registered repository URL at run time, and only when the remote's effective URL is https on that
URL's host; otherwise git uses the server's own git access (for example SSH keys an administrator registered on the
server). It never uses a personal access token. The MCP `git_push` tool pushes with your personal access token
instead; see [Forge and write tools](forge-and-write-tools.md#git-credentials).
The server accepts these writes from
`power_user` and `admin` accounts (`repository:write`); `git reset`, `git clean` and `git branch-delete` need
`admin` (`repository:admin`). Options: [files](../reference/cli/files.md#cidx-files),
[git](../reference/cli/git.md#cidx-git). CI runs: [cicd](../reference/cli/cicd.md#cidx-cicd).

## Administration and elevation

`cidx admin` covers users, groups, golden repositories, jobs, API keys and MCP credentials
([admin reference](../reference/cli/admin.md#cidx-admin)):

```bash
cidx admin repos add https://git.example.com/team/example-repo.git example-repo --default-branch main
cidx admin repos list
cidx admin users list
cidx admin groups list
```

When the server enforces TOTP step-up elevation (the elevation contract is described in
[Login and elevation](../server/auth/login-and-elevation.md#step-up-elevation)), the `cidx admin users` and
`cidx admin groups` subcommands handle it themselves (`with_elevation_retry` in
`src/code_indexer/api_clients/elevation.py`):

| Server answer | What the CLI does |
|---------------|-------------------|
| `elevation_required` | prompts `Enter your TOTP code to elevate`, calls `POST /auth/elevate`, then retries the command once |
| `totp_setup_required` | prints `TOTP setup required. Visit <setup_url> to configure your authenticator.` and exits with status 1 |
| `elevation_failed` (wrong or reused code) | prints `Invalid TOTP code. Elevation failed.` and exits with status 1 |

Other commands that reach an elevation-protected endpoint (for example `cidx admin repos add`, which calls
`POST /api/admin/golden-repos`) do not prompt: they report the server's error. With enforcement off, no command is
asked for a code.

## Known limitations

- `cidx git reset --mode hard --confirm`, `cidx git clean --confirm` and `cidx git branch-delete --confirm` do not
  complete the server's confirmation-token step: the server answers the first call with a token and changes
  nothing, and the CLI does not send the token back. Use the MCP tools, which expose the two-call handshake (see
  [Forge and write tools](forge-and-write-tools.md#destructive-git-operations-and-confirmation-tokens)).
- A remote single-repository query ignores the options listed under [What is sent](#querying-in-remote-mode).

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `Command '<name>' is not available in ...` | The directory is in the wrong mode for that command. `cidx status` shows the mode; see [What runs where](#what-runs-where). |
| `Remote initialization requires --username and --password` | Pass both options to `cidx init --remote`. |
| `Remote initialization failed: ...` | Check the URL (including `https://`), network access and the credentials; nothing was written. |
| The first query fails to link a repository | The `origin` URL or the current branch does not match a repository on the server. Run `cidx repos available` and activate the repository, or switch to a matching branch. |
| `cidx repos activate` answers 403 | The account is a `normal_user`; activation through this command needs `power_user` or `admin`. |
| `cidx git push` is rejected by the forge | The push uses the server's own git access, not your token. Ask an administrator which SSH key the server uses for that host, or push through the MCP `git_push` tool with your own token. |
| `TOTP setup required` | Enrol TOTP at the printed setup URL, then rerun the command. |

## Related

- [Operating modes](../getting-started/operating-modes.md): CLI, daemon, remote client and server compared.
- [Query guide](query.md): search modes, filters and reranking.
- [REST API](../reference/rest-api.md): the endpoints these commands call.
