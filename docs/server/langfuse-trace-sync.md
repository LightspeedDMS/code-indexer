# Langfuse Trace Sync

How a CIDX Server pulls AI conversation traces from Langfuse projects, stores them as files, and indexes them so
they can be searched like code.

Code: `src/code_indexer/server/services/langfuse_trace_sync_service.py` (sync),
`src/code_indexer/server/services/langfuse_readme_generator.py` (README files),
`register_langfuse_golden_repos` in `src/code_indexer/server/startup/bootstrap.py` (registration).

Trace **import** (this page) is separate from trace **export**, where the server sends its own traces to Langfuse.
They have separate settings sections in the Web UI.

## Configure

Runtime settings, Web UI, Configuration, Observability, **Langfuse Trace Import** (stored in `langfuse_config`):

| Setting | Default | Allowed | Meaning |
|---------|---------|---------|---------|
| `pull_enabled` | `false` | | Master switch for importing |
| `pull_host` | `https://cloud.langfuse.com` | | Langfuse API host (self-hosted instances too) |
| `pull_projects` | empty | | One entry per Langfuse project: `public_key` and `secret_key` |
| `pull_sync_interval_seconds` | `300` | 60 to 3600 (clamped) | Time between sync cycles |
| `pull_trace_age_days` | `30` | 1 to 365 (clamped) | Oldest traces fetched |
| `pull_max_concurrent_observations` | `5` | 1 to 20 (clamped) | Parallel observation fetches per project |

Project secrets are write-only: the form never shows them, and a project saved with a blank secret keeps the secret
already stored for the same public key. A save is refused, with nothing changed, when two projects share a public
key or a project would end up without a secret key. The project's name is read from Langfuse with its keys.

### When syncing runs

- **Standalone server:** the sync loop starts at server start when `pull_enabled` is on.
- **Cluster:** only the leader node syncs. A node that becomes leader starts the loop if `pull_enabled` is on at that
  moment, and stops it when it loses leadership.

The loop checks `pull_enabled` and the interval before every cycle, so turning import off stops syncing at the next
cycle. Turning it on while the server runs does not start a loop that was not started: restart the server (or use
the manual trigger below for a single sync).

## How a sync cycle works

For each configured project:

1. Fetch traces updated since the last sync minus a 2-hour overlap window, never older than `pull_trace_age_days`.
   The first sync fetches everything inside the age limit.
2. For each trace, skip it when its `updatedAt` matches the stored state and its file exists. Otherwise fetch its
   observations and compare a SHA-256 hash of the trace plus observations with the stored hash; write the file only
   when the content changed or the file is missing.
3. Trigger a refresh of each repository that received new or changed traces.

After all projects, new trace folders are registered as golden repositories and README files are regenerated for the
folders that changed. Each cycle appears in the jobs list as a `langfuse_sync` job.

## Storage layout

Under the server data directory (`~/.cidx-server/data/` by default):

```
golden-repos/
  langfuse_<project>_<userId>/          one repository per project and Langfuse user
    README.md                           index of sessions
    <sessionId>/
      README.md                         session summary
      001_turn_<last 8 chars of trace id>.json
      002_subagent-<name>_<...>.json    traces named "subagent:<name>" in Langfuse
      ...
  .langfuse_state/
    langfuse_sync_state_<project>.json  sync position and per-trace hashes
.langfuse_staging/                      temporary files for new traces
```

- Traces without a user go to `langfuse_<project>_no_user`, traces without a session to `no_session`.
- Characters that are not valid in file names are replaced by `_` in project, user and session names.
- New trace files in a session folder are numbered in trace-timestamp order, continuing after the highest number
  already in the folder; an updated trace keeps its file name.
- Each file is JSON with `trace` (the Langfuse trace, including `input` and `output`) and `observations`, ordered
  by start time.

## Repositories and search

Each `langfuse_<project>_<userId>` folder is registered as a golden repository with that alias and indexed like any
other; it is queried through its global alias `langfuse_<project>_<userId>-global`. Access follows the normal group
grants ([Accounts and Access](auth/accounts-and-access.md#groups-and-repository-access)).

MCP examples:

```
search_code(query_text="authentication error handling", repository_alias="langfuse_*")
search_code(query_text="SQL query generation", repository_alias="langfuse_ExampleProject_*")
```

A wildcard pattern is expanded against the global repositories the caller can access. See the
[Query Guide](../guides/query.md) for search parameters.

## Monitoring and manual sync

When import is on, the admin dashboard shows a Langfuse Trace Sync card (refreshed every 30 seconds) with:

- health, last sync time and duration, interval;
- per project: traces checked, new, updated and unchanged, errors and change rate;
- storage: total traces, user folders and size;
- a button for an immediate sync.

The button calls `POST /admin/langfuse-sync/trigger` (step-up elevation required when enforcement is on). It answers
HTTP 409 when a sync is already running and 503 when the sync service is not initialised.
