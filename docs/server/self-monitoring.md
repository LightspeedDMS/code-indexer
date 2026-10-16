# Self-Monitoring

Self-monitoring is a scheduled scan in which the CIDX Server has the Claude CLI read its own recent error and
warning logs, check the source code involved, and file GitHub issues for the problems it finds. This page is for
administrators: what a scan does, where its issues go, how it is scheduled, how to configure it and what bounds its
cost.

Code: `src/code_indexer/server/self_monitoring/` (`service.py` schedules, `scanner.py` runs a scan,
`issue_manager.py` files issues, `log_query.py` is the log-reading command); start-up wiring in
`src/code_indexer/server/startup/lifespan.py`; the page in `src/code_indexer/server/web/routes.py`.

## What a scan does

1. Reads the highest log id processed by the most recent successful scan (0 before the first one).
2. Fetches up to 100 open issues of the target GitHub repository, for duplicate detection.
3. Builds the prompt from `src/code_indexer/server/self_monitoring/prompts/default_analysis_prompt.md`, filling in
   the last processed log id, the duplicate-detection context (those open issues plus the fingerprints of issues
   this server filed in the last 90 days) and the log-query command. The prompt is read from that file at every
   scan; it has no Web UI setting.
4. Runs the Claude CLI once, in the server's repository directory, with the configured model and a 30-minute limit.
5. Files one GitHub issue per problem the answer lists, and records each in the database.
6. Records the scan. A successful scan stores the highest log id the answer reports as processed, so the next scan
   starts after it; a failed scan stores nothing, so the next scan covers the same entries again.

### What it analyses

The prompt directs the model to:

- read log entries with an id above the last processed id and level `ERROR`, `WARNING` or `CRITICAL`;
- treat a warning whose first 80 characters and source repeat 5 or more times as a possible stuck state, filed as a
  `server_bug` titled "Repeating warning: ...";
- open the source file named by each entry and decide whether the error is already handled (not a bug) or not;
- classify each issue as `server_bug` (title prefix `[BUG]`), `client_misuse` (`[CLIENT]`) or `documentation_gap`
  (`[DOCS]`), and skip duplicates of existing issues;
- answer with JSON listing the issues and the highest log id processed.

### What the Claude session can access

Built by `build_self_monitoring_claude_args()` (`services/agent_cli_isolation.py`):

- its only allowed Bash command is `<server python> -m code_indexer.server.self_monitoring.log_query "<SQL>"`. That
  command opens the log database read-only, runs one `SELECT` per call, returns at most 200 rows and stops a query
  after 60 seconds. The database path comes from an environment variable the server sets, not from the command;
- file reads are limited to the working directory (the server's repository).

The scan always runs on the Claude CLI; it is never dispatched to Codex.

## Where issues go

- **Repository.** At startup the server finds its repository directory (`CIDX_REPO_ROOT`, set in the installed
  systemd unit, or the directory the code runs from) and takes `owner/repo` from `git remote get-url origin`. Issues
  are filed in that GitHub repository. If no repository or remote is found, the service does not start (log code
  `MONITOR-GENERAL-010`) and **Run now** answers with an error.
- **Credentials.** The GitHub token stored in Configuration, GitHub/GitLab Keys. Scheduled scans use the token read
  at server startup; a manual scan reads the current one.
- **Content.** Issues are created through the GitHub REST API. Each body starts with the server's
  `service_display_name` and the scan id, followed by the text the model wrote from the log entries and code it read.
  Everyone who can read the target repository can read these issues.
- **Records.** Scans are recorded in `self_monitoring_scans` and filed issues (number, URL, classification, error
  codes, source log ids and files, fingerprint) in `self_monitoring_issues`: the server database on a standalone
  server, PostgreSQL in a cluster. The Self-Monitoring page lists both.

## Scheduling

- Self-monitoring is off by default. When it is enabled at startup and the repository was found, the service is
  created at startup.
- **Standalone**: the service starts in each server process at startup, so with `workers` above 1 each worker
  process runs its own schedule.
- **Cluster**: at startup the service starts only on the leader node, through the leader-election callbacks, and
  stops when the node loses leadership. A Web UI save acts on the node that handles it: enabling there starts a
  schedule on that node as well, and disabling there does not stop the leader's schedule.
- **Cadence**: the first scan waits for the rest of the cadence interval counted from the start of the last recorded
  scan (or runs at once if none or overdue); then a scan is submitted every `cadence_minutes`.
- Each scan runs as a background job of type `self_monitoring` (user `system`), visible on the jobs dashboard
  ([Maintenance and Jobs](maintenance-and-jobs.md)).
- Before submitting a scan, scans started more than 2 hours ago that never finished are marked `FAILURE`
  ("Scan failed to complete (orphaned after 2 hours)").
- **Run now** (`POST /admin/self-monitoring/run-now`) queues one scan immediately with the current settings. It is
  refused while self-monitoring is disabled.

The scan reads the SQLite log database `logs.db` in the server data directory. In a cluster, application logs are
written to PostgreSQL ([Observability](observability.md#where-they-are)), not to that file.

## Configuration

Self-Monitoring page in the administration Web UI (`/admin/self-monitoring`, admin session; saving and **Run now**
require an open elevation window when elevation enforcement is on). The values are runtime settings in the object
`self_monitoring_config`:

| Setting | Default | Choices on the page | Meaning |
|---------|---------|---------------------|---------|
| `enabled` | `false` | checkbox | Turns scheduled scans on or off |
| `cadence_minutes` | `60` | 15, 30, 60, 360, 1440 | Interval between scheduled scans |
| `model` | `opus` | `opus`, `sonnet` (the save route also accepts `haiku`) | Claude model of the scan |

How a save takes effect: it stores the values, then starts or stops the service of the server process that handled
the request, provided that process created the service at startup. Enabling self-monitoring on a server where it was
disabled at startup, and changing the cadence or model of the running schedule, take effect after a restart.
**Run now** always uses the stored values.

Related settings on the Configuration screen ([Server Settings Reference](../reference/server-settings.md)):

| Setting | Effect |
|---------|--------|
| GitHub/GitLab Keys, GitHub token | Token used to read and create issues |
| `service_display_name` (Server Settings) | Server name written at the top of each issue |
| `pace_maker_mode` (Pace Maker) | Pace-maker handling before the Claude CLI runs |
| Provider API Keys (Anthropic) or Subscription Mode | Claude CLI credentials |

## Cost controls

- Off by default; nothing runs until an administrator enables it.
- `cadence_minutes` sets how often a scan runs, and `model` which Claude model it uses.
- One Claude CLI run per scan, stopped after 30 minutes.
- Each scan covers only log entries newer than the last successful scan; the log-query command returns at most 200
  rows per call.
- Duplicate detection against open issues and previously filed fingerprints avoids filing the same problem twice.
- `pace_maker_mode` applies to the scan like any other Claude CLI run.
