# Temporal Search

Search a repository's git history by meaning: find the commits whose message and changes match a query, limited
to a date range or an author. This guide covers how the history index is built and stored, the
`cidx index --index-commits` options, the query flags and how each filter behaves today.

Audience: CLI users, AI-agent integrators, and server operators enabling history search on golden repositories.
General query flags are in the [Query Guide](query.md).

## Contents

- [How the History Index Works](#how-the-history-index-works)
- [Build the Index](#build-the-index)
- [Query History](#query-history)
- [Filter Behaviour](#filter-behaviour)
- [Output](#output)
- [Server and MCP](#server-and-mcp)
- [Troubleshooting](#troubleshooting)

## How the History Index Works

**One document per commit.** For every commit, CIDX builds one aggregated document: the commit message first,
then each changed file's diff under a `--- <path> ---` header. The document is split into chunks of
`temporal.aggregation_chunk_chars` characters (default 4096) and each chunk is embedded. The first chunk of a
commit is its head chunk; it starts with the commit message. Author, date and hash are stored with every chunk.
Chunk ids have the form `<project>:commit:<hash>:<n>`.

**Embedders.** History is embedded by temporal embedder adapters, configured per repository:

| Adapter | Provider | API key |
|---------|----------|---------|
| `voyage-context-4` (default) | VoyageAI | `VOYAGE_API_KEY` |
| `embed-v4.0` | Cohere | `CO_API_KEY` |

Every adapter listed in `temporal.embedders` gets its own index. Queries use `temporal.active_embedder` unless
`--temporal-embedder` names another one. The settings live in the repository's `.code-indexer/config.json`:

```json
"temporal": {
  "active_embedder": "voyage-context-4",
  "aggregation_chunk_chars": 4096,
  "diff_context_lines": 5,
  "embedders": ["voyage-context-4"]
}
```

`active_embedder` must be one of `embedders` (`TemporalConfig`, `src/code_indexer/config.py`).

**Quarterly shards.** Each embedder's index is split by the calendar quarter of the commit date. A local repository
with both adapters configured looks like this (from a run against a three-commit repository):

```
.code-indexer/index/
  code-indexer-temporal/                         temporal_metadata.db (shared bookkeeping)
  code-indexer-temporal-voyage_context_4/        temporal_meta.json
  code-indexer-temporal-voyage_context_4-2025Q1/ chunks.db, hnsw_index.bin, temporal_progress.json, ...
  code-indexer-temporal-voyage_context_4-2025Q2/
  code-indexer-temporal-embed_v4_0-2025Q1/
  code-indexer-temporal-embed_v4_0-2025Q2/
```

Shard rows are stored in each shard's `chunks.db`. A query with `--time-range` opens only the shards whose quarter
overlaps the range, and fuses the per-shard results of the selected embedder
(`src/code_indexer/services/temporal/temporal_fusion_dispatch.py`).

On a server, a golden repository's history index is kept outside its clone, at
`{golden_repos_dir}/.temporal/{alias}/code-indexer-temporal-{embedder}-{quarter}/chunks.db`
(`src/code_indexer/services/temporal/temporal_server_paths.py`), so activated copies of the repository read the
golden repository's current history instead of a copy.

## Build the Index

```bash
cidx index --index-commits                         # current branch, all commits
cidx index --index-commits --since-date 2025-03-01 # only commits since a date
cidx index --index-commits --max-commits 1         # schedule at most N new commits this run
cidx index --index-commits --diff-context 3        # diff context lines (0-50, default 5)
cidx index --index-commits --all-branches          # every branch, not only the current one
cidx index --reconcile --index-commits --reconcile-embedder voyage-context-4
```

| Option | Effect |
|--------|--------|
| `--index-commits` | index git history for the current branch |
| `--all-branches` | index all branches; requires `--index-commits` |
| `--max-commits N` | per embedder, schedule only the newest N commits not yet indexed; the rest are picked up by a later run |
| `--since-date YYYY-MM-DD` | only commits since the date |
| `--diff-context N` | context lines in each diff, 0-50 (default 5) |
| `--reconcile --index-commits` | compare the shards on disk with history and index missing commits |
| `--reconcile-embedder NAME` | limit reconcile to one embedder; repeatable |

- `cidx index --index-commits` runs history indexing only and then exits; it does not update the HEAD (current
  code) index. Run `cidx index` for that.
- Re-running is incremental: commits already in the shards are skipped. The summary prints
  `Total commits processed` and `Skip ratio`.
- `--new-collection-layout sharded_json` is rejected with `--index-commits`; history is always written as
  `chunks.db`.

## Query History

```bash
cidx query "JWT token validation" --time-range-all --quiet
cidx query "JWT token validation" --time-range 2025-03-01..2025-03-31 --quiet
cidx query "database connector" --time-range-all --author "Alice" --quiet
cidx query "user lookup fix" --time-range-all --chunk-type commit_message --quiet
cidx query "JWT token" --time-range-all --temporal-embedder embed-v4.0 --quiet
```

| Flag | Effect |
|------|--------|
| `--time-range START..END` | commits dated in the range, `YYYY-MM-DD..YYYY-MM-DD` |
| `--time-range-all` | all indexed history |
| `--author TEXT` | commits whose author name contains TEXT (case-insensitive) |
| `--chunk-type commit_message` | only each commit's head chunk (the one that starts with the message) |
| `--chunk-type commit_diff` | all chunks; no filtering |
| `--temporal-embedder NAME` | query this embedder's index instead of `active_embedder` |
| `--limit N` | maximum results (default 10) |
| `--rerank-query`, `--rerank-instruction` | rerank results (see [Reranking](query.md#reranking)); unlike a current-code query, `--rerank-query ""` does NOT turn reranking off here: an empty value counts as omitted, so with `auto_populate_rerank_query` on (the default) the search query is used. Set `auto_populate_rerank_query` to false in the CLI config file to disable it |

A time range flag is what makes a query temporal. `--chunk-type` without one is rejected
(`--chunk-type requires --time-range or --time-range-all`). An invalid date is rejected with
`Invalid time range: Invalid date format. Use YYYY-MM-DD`. A `--temporal-embedder` with no indexed shards prints a
WARNING (`Temporal embedder '...' has no indexed collections`) and returns no results; CIDX does not fall back to
the active embedder.

Temporal queries run in local mode only. Point-in-time scoping (`at_commit`) is available through the server API,
not the CLI.

## Filter Behaviour

Observed with the current code against a scratch repository; use this table rather than the flag names to decide
what a temporal query filters.

| Flag | Behaviour in a temporal query |
|------|-------------------------------|
| `--time-range`, `--time-range-all` | filters by commit date |
| `--author` | filters by author name substring; an email address matches nothing |
| `--chunk-type` | as described above |
| `--diff-type` | accepted, does not filter: one aggregated chunk can cover files with different change kinds |
| `--language`, `--exclude-language` | ignored: the CLI does not pass them to the temporal search |
| `--min-score` | ignored: not passed to the temporal search |
| `--path-filter` | returns no results: commit chunks carry no single file path |
| `--exclude-path` | no effect, for the same reason |
| `--temporal-embedder` without a time range | ignored; the query runs as a normal semantic query |

With daemon mode enabled (`cidx config --daemon`), the temporal query sent to the daemon does not include
`--author` (`_query_temporal_via_daemon`, `src/code_indexer/cli_daemon_delegation.py`).

## Output

`--quiet` prints one line per result: rank, score, the shard it came from and a file path from the commit:

```
1. 0.536 [voyage_context_4-2025Q1] src/auth/token.py
```

Without `--quiet`, each result shows the commit hash and date, author name and email, the commit message and the
matched chunk (message and diff lines).

## Server and MCP

The server's REST `POST /api/query` and MCP `search_code` accept `time_range`, `time_range_all`, `at_commit`,
`author`, `chunk_type`, `diff_type` and `temporal_embedder` (see the
[Query Parameter Inventory](query.md#query-parameter-inventory)). `at_commit` takes a commit hash or ref, resolves it
with git and limits results to commits at or before it; an unresolvable ref is an error (HTTP 400 on REST).

History search on a golden repository is enabled when the repository is added: `add_golden_repo` (MCP) takes
`enable_temporal` and `temporal_options` (`max_commits`, `since_date`, `diff_context`). By default only the
registered branch is indexed. Indexing all branches is gated by the runtime setting
`temporal_all_branches_enabled` (default off); while it is off, a request with `all_branches=true` is rejected.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Temporal index not available for this repository` | run `cidx index --index-commits` |
| new commits missing from results | re-run `cidx index --index-commits`; it indexes only commits not yet stored |
| a filter seems to do nothing | check [Filter Behaviour](#filter-behaviour) |
| results only from one embedder | queries use `active_embedder`; pass `--temporal-embedder` for another |
| `Cannot use --all-branches without --index-commits` | add `--index-commits` |

## Related

- [Query Guide](query.md)
- [Architecture Invariants](../architecture/invariants/indexing-and-migrations.md) (indexing and migrations)
- [Configuration](../getting-started/configuration.md)
