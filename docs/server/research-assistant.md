# Research Assistant

The Research Assistant is a chat page in the administration Web UI (`/admin/research`) in which an administrator
directs a Claude CLI session, running on the server, to investigate and repair problems of the server's own
environment and data. This page describes who can use it, what the session is set up to do, where conversations are
kept, how it behaves in a cluster, and its settings. A short summary is in
[Operating Modes: Research Assistant](../getting-started/operating-modes.md#research-assistant).

It is not a read-only tool: the session is given remediation authority over the server's environment and data.

Code: `src/code_indexer/server/routers/research_assistant.py`,
`src/code_indexer/server/services/research_assistant_service.py`,
`src/code_indexer/server/services/research_cleanup_service.py`.

## Access

Every route requires an admin Web UI session (`require_admin_session`); API keys and bearer tokens do not open it.
When step-up elevation enforcement is on ([Login and Elevation](auth/login-and-elevation.md#step-up-elevation)),
actions that change something also require an open elevation window:

| Action | Route | Elevation |
|--------|-------|-----------|
| Open the page | `GET /admin/research` | no |
| Send a message | `POST /admin/research/send` | yes |
| Poll for the answer | `GET /admin/research/poll/{job_id}` | no |
| Create, rename, delete a session | `POST /admin/research/sessions`, `PUT` / `DELETE /admin/research/sessions/{session_id}` | yes |
| Load a session | `GET /admin/research/sessions/{session_id}` | no |
| Upload a file | `POST /admin/research/sessions/{session_id}/upload` | yes |
| List, download files | `GET /admin/research/sessions/{session_id}/files[/{filename}]` | no |
| Delete a file | `DELETE /admin/research/sessions/{session_id}/files/{filename}` | yes |

Sessions are not per administrator: every administrator sees and can continue every session.

## What the session can and cannot do

Each message runs the Claude CLI once (`_run_claude_background`). What follows is what the invocation and its
permission settings declare.

- **Tools offered**: Bash, Read, Glob, Grep, Write, Edit and TodoWrite. WebFetch, WebSearch, Agent, Skill and
  NotebookEdit are disallowed.
- **Permission settings** (passed with `--settings`, built by `_build_permission_settings`):
  - allow: Read, Glob, Grep, TodoWrite, an Edit rule scoped to the cidx-meta directory
    (`<server data dir>/golden-repos/cidx-meta`: repository descriptions and dependency maps), and three wrapper
    scripts from the server's repository: `scripts/cidx-meta-cleanup.sh`, `scripts/cidx-db-query.sh` (database
    queries) and `scripts/cidx-curl.sh` (HTTP requests). The wrapper rules are added only when the repository root
    is known (`CIDX_REPO_ROOT` or auto-detection);
  - deny: the Write, Edit, WebFetch and WebSearch tools, plus the Bash rules below (`_bash_deny_rules`).
- **Bash denied**: network tools (`curl`, `wget`, `ssh`, `scp`, `nc`, `nmap`, `rsync` and others), interpreters
  (`python`, `perl`, `ruby`, `node`, `php`, `lua`), nested shells and `exec`/`eval`, `xargs` and `find`, privilege
  escalation (`sudo`, `su`, `doas`), `chgrp` and `install`, `git config`, package managers, service control
  (`systemctl start|stop|enable|disable|reload`, `service`), git writes (`push`, `commit`, `checkout`, `reset`,
  `rebase`, `merge`, `stash`, `clean`, `restore`), `tee`, `killall`, disk and mount commands, `crontab` and `at`.
- **Bash left allowed** for repairs: `rm`, `mv`, `cp`, `mkdir`, `rmdir`, `touch`, `chmod`, `chown`, `ln`, `kill`
  and `pkill`, so the session can repair corrupted indexes, orphaned metadata or stuck jobs.
- **Edit scope**: file edits are intended only inside cidx-meta; Write and Edit are denied everywhere else.
- **Network scope**: direct `curl` is denied; HTTP requests go through `scripts/cidx-curl.sh`, which allows only
  loopback plus operator-configured CIDR ranges (`ra_curl_allowed_cidrs`, which has no Web UI control).
- **Working directory**: `~/.cidx-server/research/<session id>/` on the node that runs the message. It contains a
  `code-indexer` link to the server's repository and an `issue_manager.py` link used to file GitHub issues.
- **Environment**: `GITHUB_TOKEN` and `GH_TOKEN` are set when a GitHub token is stored (Configuration, GitHub/GitLab
  Keys); `CIDX_SERVER_DATA_DIR`, `CIDX_REPO_ROOT` and `CIDX_META_BASE` locate the server's data for the wrapper
  scripts.
- **Instructions**: the first message of a session is prefixed with the prompt template
  `src/code_indexer/server/config/research_assistant_prompt.md`, filled in with the server's paths and version. It
  tells the session to repair environment and data problems, to report source-code bugs as GitHub issues instead of
  changing code, and to treat third-party provider problems as report-only. If the template cannot be read, the
  message fails rather than running without it.
- **Pacing**: before each run the server applies the `pace_maker_mode` setting
  ([Auto-Update: pace-maker](auto-update.md#pace-maker)).

Every run is limited by `research_assistant_timeout_seconds` (default 1200); a run that exceeds it is reported as
"Claude CLI execution timed out".

## Sessions, messages and files

| Data | Where | Kept until |
|------|-------|------------|
| Sessions (`id`, `name`, `folder_path`, timestamps, the Claude session id) | `research_sessions` table | The session is deleted |
| Messages | `research_messages` table | The session is deleted (deleted with it) |
| Working directory and uploads | `~/.cidx-server/research/<session id>/` on each node that ran or received them | Session deletion on the node that handles it, or the workspace cleanup below |

- The tables are in the server database (`cidx_server.db`) on a standalone server and in PostgreSQL in a cluster.
- A session named "Default Session" (id `default`) is created on first use.
- The user's text is stored as typed; the prompt template prefixed to the first message is not stored. The
  assistant's answer is stored when the run succeeds; a failed run shows its error and stores no answer.
- Each session has its own Claude session id. Later messages resume that Claude conversation; when the Claude CLI no
  longer has it, the message starts a new Claude conversation under the same id.
- Messages are rendered as sanitized HTML.

**Uploads.** A file attached to a session is stored in `uploads/` of the working directory, where the Claude session
can read it. Accepted extensions: `.txt`, `.log`, `.json`, `.yaml`, `.yml`, `.py`, `.md`, `.csv`, `.xml`, `.html`,
`.cfg`, `.conf`, `.ini`. Limits: 10 MB per file, 100 MB per session.

**Deleting a session** removes its row and messages, its working directory on the node that handles the request, and
the Claude CLI's project data for that directory.

**Workspace cleanup.** An hourly sweep (also run at startup) deletes working directories under
`~/.cidx-server/research/` that have no matching session row, are older than `research_session_retention_days`
(default 7) and have not been modified for 24 hours. Only directories named like a session id are candidates;
`default` is never deleted. If the list of live sessions cannot be read, the sweep deletes nothing.

## Cluster behaviour

- A message runs in a background thread of the server process that received `POST /admin/research/send`. The run
  is registered in the job tracker as job type `research_assistant_chat` (user `system`, repository `server`), so it
  appears on the jobs dashboard ([Maintenance and Jobs](maintenance-and-jobs.md)).
- The page polls `GET /admin/research/poll/{job_id}`. A poll served by a process or node other than the one running
  the message reads the job's status from the job tracker (PostgreSQL in a cluster) and the answer from the shared
  `research_messages` table. Seeing results therefore does not need session affinity at the load balancer.
- The working directory, uploaded files and the Claude CLI's own conversation state are local to the node that ran
  a message. A follow-up message handled by another node runs in that node's directory for the session: files
  uploaded through another node are not there, and the earlier conversation context is not available to the Claude
  CLI on that node. Routing an administrator's Web UI traffic to one node keeps a conversation and its uploads
  together.

## Configuration

| Setting | Where | Default | Effect |
|---------|-------|---------|--------|
| `analysis_model` | Configuration, Golden Repository Settings | `opus` | Claude model of the session; read for each message |
| `research_assistant_timeout_seconds` | Configuration, Claude CLI Integration | `1200` | Limit for one run (the form accepts 60 to 7200) |
| `pace_maker_mode` | Configuration, Pace Maker | `disabled` | Pace-maker handling before each run |
| Anthropic credentials | Configuration, Provider API Keys or Subscription Mode | none | How the Claude CLI authenticates |
| GitHub token | Configuration, GitHub/GitLab Keys | none | Exported to the session for filing GitHub issues |
| `research_session_retention_days` | no Web UI control | `7` | Age before an orphaned working directory is deleted |
| `ra_curl_allowed_cidrs` | no Web UI control | empty | Extra CIDR ranges `scripts/cidx-curl.sh` allows besides loopback |

Details of each setting: [Server Settings Reference](../reference/server-settings.md).
