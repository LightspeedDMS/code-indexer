# Langfuse Trace Sync

Automatically pull AI conversation traces from Langfuse and make them semantically searchable. CIDX syncs traces in the background, indexes them with the same semantic search engine used for code, and makes them available via MCP tools and CLI queries.

## Table of Contents

- [Overview](#overview)
- [How It Works](#how-it-works)
- [Storage Layout](#storage-layout)
- [Searching Traces](#searching-traces)
- [Dashboard Monitoring](#dashboard-monitoring)
- [Configuration](#configuration)

## Overview

Langfuse Trace Sync bridges the gap between AI conversation history and code search. Once enabled, CIDX continuously pulls traces from configured Langfuse projects, indexes them alongside your code, and makes the full conversation history -- prompts, responses, tool calls -- semantically searchable.

## How It Works

1. **Background sync**: Pulls traces from configured Langfuse projects at a configurable interval (default: 5 minutes)
2. **Smart deduplication**: Overlap window + content hash strategy detects trace mutations without re-downloading unchanged data
3. **Auto-registration**: New trace folders are automatically registered as golden repos and indexed
4. **Watch integration**: File system watchers trigger incremental re-indexing as new traces arrive

## Storage Layout

Traces are stored as JSON files organized by project, user, session, and trace ID:

```
golden-repos/
  langfuse_<project>_<userId>/
    <sessionId>/
      <traceId>.json    # Full trace + observations, chronologically ordered
```

Each trace file contains the user prompt (`trace.input`), AI response (`trace.output`), metadata, and all observations (tool calls) in chronological order.

## Searching Traces

### Via MCP

```
search_code("authentication error handling", repository_alias="langfuse_*")
search_code("SQL query generation", repository_alias="langfuse_MyProject_*")
```

### Via CLI

```bash
cidx query "authentication error handling" --repo "langfuse_*"
```

The `langfuse_*` wildcard matches all Langfuse trace repositories. Use `langfuse_<project>_*` to scope to a specific project.

## Dashboard Monitoring

The admin dashboard provides real-time sync health monitoring:

- Per-project metrics: traces checked, new, and updated counts
- Storage statistics: total traces, disk usage
- Manual sync trigger for on-demand pulls
- Sync error reporting with last-success timestamps

## Configuration

Enable via the Web UI Config Screen under Langfuse settings. Requires a Langfuse project public/secret key pair.

| Setting | Description | Default |
|---------|-------------|---------|
| `langfuse_sync_enabled` | Enable background trace sync | `false` |
| `langfuse_sync_interval_seconds` | Seconds between sync cycles | `300` (5 min) |
| `langfuse_public_key` | Langfuse project public key | -- |
| `langfuse_secret_key` | Langfuse project secret key | -- |
| `langfuse_host` | Langfuse API host | `https://cloud.langfuse.com` |
