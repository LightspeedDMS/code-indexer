# Temporal Search - Git History Search

Search your entire git commit history semantically with CIDX temporal queries.

## Table of Contents

- [Overview](#overview)
- [Setup](#setup)
- [Basic Usage](#basic-usage)
- [Query Parameters](#query-parameters)
- [Use Cases](#use-cases)
- [Examples](#examples)
- [Performance](#performance)
- [Troubleshooting](#troubleshooting)

## Overview

Temporal search allows you to **semantically search your git history** - find when code was added, modified, or deleted based on conceptual queries, not just text matching.

**What Makes It Unique**:
- **Semantic search** across commits and diffs (not just grep)
- **Time-range filtering** - search specific date ranges
- **Author filtering** - find changes by specific developers
- **Diff type filtering** - added, modified, deleted, renamed, binary
- **Chunk type selection** - search commit messages or code diffs

**Use Cases**:
- Code archaeology - "When was OAuth added?"
- Bug history - "Find security patches from last quarter"
- Feature evolution - "How did authentication change over time?"
- Author analysis - "What did the security team work on?"

## Setup

### 1. Index Git Commits

Temporal search requires indexing your git history first:

```bash
# One-time setup (indexes all commits)
cidx index --index-commits

# This creates per-embedder, quarterly-sharded temporal indexes, e.g.:
# .code-indexer/index/code-indexer-temporal-voyage_context_4-2024Q3/
# .code-indexer/index/code-indexer-temporal-embed_v4_0-2024Q3/
```

**What Gets Indexed**: Each commit is aggregated into ONE per-commit document
(the commit message once, at the head, followed by each changed file's diff
under a `--- <path> ---` header). That aggregated document is chunked and
embedded under every configured temporal embedder adapter (`temporal.embedders`
in config, e.g. `voyage-context-4` and/or `embed-v4.0`) -- NOT as separate
"commit message" and "code diff" vectors. Commit metadata (author, date,
hash) rides in each chunk's payload; the `--diff-type` CLI filter is a
documented no-op post-aggregation (a single chunk can span multiple files
with different diff kinds -- see Diff Types below).

**Indexing Time**:
- Small repos (<100 commits): Seconds
- Medium repos (100-1000 commits): 1-5 minutes
- Large repos (1000+ commits): 5-30 minutes

Re-running `cidx index --index-commits` is safe and cheap: a disk-scan-based
reconcile step skips every already-indexed commit (per embedder, per
quarterly shard) and only embeds genuinely new or missing commits.

### 2. Verify Temporal Index

```bash
# Check if a temporal index exists for the active embedder (model slug varies
# by embedder, e.g. voyage_context_4 or embed_v4_0)
ls -lh .code-indexer/index/ | grep code-indexer-temporal

# Each quarterly shard directory (e.g. code-indexer-temporal-voyage_context_4-2024Q3)
# holds its own HNSW index, id index, and per-commit vector payload files.
```

## Basic Usage

### Simple Temporal Search

```bash
# Search all git history
cidx query "JWT authentication" --time-range-all --quiet

# Always use --quiet for cleaner output
```

**Output**:
- Commits containing matching code/messages
- File paths and line numbers
- Commit hashes and authors
- Timestamps

### Time Range Search

```bash
# Specific date range (YYYY-MM-DD..YYYY-MM-DD)
cidx query "security fix" --time-range 2024-01-01..2024-12-31 --quiet

# Last quarter
cidx query "refactoring" --time-range 2024-10-01..2024-12-31 --quiet

# Specific month
cidx query "new feature" --time-range 2024-11-01..2024-11-30 --quiet
```

## Query Parameters

### Core Temporal Parameters

| Parameter | CLI Flag | Type | Description |
|-----------|----------|------|-------------|
| **time_range** | --time-range RANGE | string | Date range (YYYY-MM-DD..YYYY-MM-DD) |
| **time_range_all** | --time-range-all | flag | Search all git history |
| **diff_type** | --diff-type TYPE | string | Filter by diff type |
| **author** | --author NAME | string | Filter by commit author |
| **chunk_type** | --chunk-type TYPE | enum | commit_message or commit_diff |

**Note**: Always use `--quiet` flag with temporal queries for cleaner output.

### API-Only Parameters

These parameters are only available via REST/MCP API (not CLI):

| Parameter | Type | Description |
|-----------|------|-------------|
| **at_commit** | string | Query code at specific commit hash |
| **include_removed** | boolean | Include files removed from HEAD |
| **show_evolution** | boolean | Show code evolution timeline |
| **evolution_limit** | integer | Limit evolution entry count |

### Time Range Format

```bash
# Full date range
--time-range 2024-01-01..2024-12-31

# All history
--time-range-all
```

**Date Format**: YYYY-MM-DD (ISO 8601)

### Diff Types

| Type | Description | Use Case |
|------|-------------|----------|
| **added** | Newly added code | "When was feature X added?" |
| **modified** | Changed code | "Recent changes to auth module" |
| **deleted** | Removed code | "What was removed during refactor?" |
| **renamed** | Renamed files | "File name changes" |
| **binary** | Binary file changes | "Binary asset updates" |

```bash
# Find when code was added
cidx query "OAuth integration" --time-range-all --diff-type added --quiet

# Find modifications
cidx query "password validation" --time-range-all --diff-type modified --quiet

# Find deletions
cidx query "legacy code" --time-range-all --diff-type deleted --quiet
```

**Multiple Diff Types** (API only):
```json
{
  "diff_type": "added,modified"
}
```

**Note**: under the per-commit aggregated model, `--diff-type` is a
documented no-op -- a single chunk can span multiple changed files with
DIFFERENT diff kinds (added/modified/deleted/renamed) within the same
commit, so filtering by a single diff type at the chunk level would be
ambiguous. The flag is accepted for backward compatibility but does not
filter results.

### Author Filtering

```bash
# Filter by email
cidx query "feature work" --time-range-all --author "dev@example.com" --quiet

# Filter by name (partial match)
cidx query "bug fixes" --time-range-all --author "John" --quiet

# Filter by team alias
cidx query "security updates" --time-range-all --author "security-team" --quiet
```

**Author Matching**:
- Matches commit author name OR email
- Partial matches supported ("John" matches "John Doe", "johnny@example.com")
- Case-insensitive

### Chunk Types

| Type | Description | Use Case |
|------|-------------|----------|
| **commit_message** | Search commit messages only | Find tickets, keywords in messages |
| **commit_diff** | Search code diffs only | Find code changes, not messages |

```bash
# Search commit messages (find tickets, keywords)
cidx query "JIRA-123" --time-range-all --chunk-type commit_message --quiet

# Search code diffs (find actual code changes)
cidx query "authentication logic" --time-range-all --chunk-type commit_diff --quiet
```

**Default**: Both commit messages and diffs are searched if chunk_type not specified.

**Note**: under the per-commit aggregated model the commit message is never
embedded as its own separate, message-only vector. The commit message forms
the HEAD of each commit's single aggregated document (message once,
followed by every changed file's diff); `chunk_type=commit_message` filters
to that head chunk, and `chunk_type=commit_diff` returns all chunks (no
filtering) -- it does not select a distinct "diff-only" vector type.

## Use Cases

### 1. Code Archaeology

**"When was this feature added?"**

```bash
# Find when JWT authentication was added
cidx query "JWT token validation" --time-range-all --diff-type added --quiet

# Find initial OAuth implementation
cidx query "OAuth integration" --time-range-all --diff-type added --quiet
```

### 2. Bug History Tracking

**"Find all security patches from last quarter"**

```bash
# Security fixes in Q4 2024
cidx query "security vulnerability fix" \
  --time-range 2024-10-01..2024-12-31 \
  --chunk-type commit_message \
  --quiet

# Find XSS patch commits
cidx query "XSS protection" --time-range-all --diff-type modified --quiet
```

### 3. Feature Evolution

**"How did authentication change over time?"**

```bash
# Find all authentication-related commits
cidx query "authentication" --time-range-all --quiet

# Find auth changes in 2024
cidx query "authentication" --time-range 2024-01-01..2024-12-31 --quiet

# Find auth refactoring
cidx query "auth refactor" --time-range-all --chunk-type commit_message --quiet
```

### 4. Author Analysis

**"What did the security team work on?"**

```bash
# All security team commits
cidx query "security" --time-range-all --author "security-team" --quiet

# Specific developer's work
cidx query "feature implementation" --time-range-all --author "jane@example.com" --quiet

# Team contributions in date range
cidx query "new features" \
  --time-range 2024-11-01..2024-11-30 \
  --author "backend-team" \
  --quiet
```

### 5. Refactoring Analysis

**"What was removed during the refactor?"**

```bash
# Find deleted code
cidx query "legacy authentication" --time-range-all --diff-type deleted --quiet

# Find refactoring commits
cidx query "refactor" --time-range 2024-01-01..2024-12-31 \
  --chunk-type commit_message \
  --quiet
```

### 6. Ticket/Issue Tracking

**"Find all work related to JIRA-123"**

```bash
# Search commit messages for ticket number
cidx query "JIRA-123" --time-range-all --chunk-type commit_message --quiet

# Find related code changes
cidx query "JIRA-123" --time-range-all --chunk-type commit_diff --quiet
```

## Examples

### Combine Multiple Filters

```bash
# Complex temporal query:
# Find auth changes by security team in Q4 2024
cidx query "authentication" \
  --time-range 2024-10-01..2024-12-31 \
  --author "security-team@example.com" \
  --diff-type modified \
  --chunk-type commit_diff \
  --language python \
  --quiet
```

### Recent Changes

```bash
# Find changes from last 30 days
cidx query "bug fix" --time-range 2024-12-01..2024-12-31 --quiet

# Find this month's features
cidx query "new feature" --time-range 2024-12-01..2024-12-31 \
  --chunk-type commit_message \
  --quiet
```

### Specific File History

```bash
# Combine with path filtering
cidx query "auth changes" \
  --time-range-all \
  --path-filter "*/auth/*" \
  --quiet
```

## Performance

### Indexing Performance

| Repository Size | Commits | Index Time |
|----------------|---------|------------|
| Small | <100 | <30 seconds |
| Medium | 100-1000 | 1-5 minutes |
| Large | 1000-10000 | 5-30 minutes |
| Very Large | 10000+ | 30+ minutes |

**Optimization Tips**:
- Index once, query many times
- Incremental indexing updates only new commits
- Use `--index-commits` on initial setup only

### Query Performance

| Query Type | Performance | Notes |
|------------|-------------|-------|
| **Simple temporal** | ~200-500ms | All history, no filters |
| **Time range** | ~100-300ms | Filtered by date |
| **With author filter** | ~150-400ms | Additional filtering |
| **Complex (multiple filters)** | ~300-800ms | Multiple filter overhead |

**Performance Factors**:
- Repository size (commit count)
- Time range breadth
- Number of filters applied
- Semantic complexity of query

### Storage Impact

Temporal indexing increases storage. The per-commit aggregated model
produces roughly ONE vector per commit (versus multiple per-file-diff
vectors under the old layout), so per-commit storage is dramatically lower
for commits touching many files with small changes each (see
`scripts/analysis/temporal_vector_projection.py` for a git-history-derived
projection on your own repo):

| Repository | Additional Storage (per embedder) |
|------------|-------------------|
| Small (<100 commits) | ~1-5 MB |
| Medium (100-1000) | ~5-50 MB |
| Large (1000-10000) | ~50-500 MB |
| Very Large (10000+) | ~500MB-2GB |

**Storage Location**: `.code-indexer/index/code-indexer-temporal-{model_slug}-{YYYY}Q{N}/` -- one directory per configured embedder (`model_slug`, e.g. `voyage_context_4`, `embed_v4_0`) PER CALENDAR QUARTER the indexed commits fall in. Each shard directory holds its own `temporal_progress.json` (per-commit completion tracking), `temporal_structure.json` (v2 layout marker), HNSW index, ID index, and per-commit vector payload files.

**Why quarterly sharding is retained after per-commit aggregation**: per-commit aggregation already delivered the primary vector-count reduction (many per-file-diff vectors collapsed into ~1 vector per commit); quarterly sharding is an ORTHOGONAL, complementary optimization for repos with a long commit history:

- **Expected shard counts stay small and bounded**: a repository active for N years produces at most `4*N` shard directories per embedder, growing linearly with calendar time regardless of commit volume or aggregation strategy -- a 10-year-old, 100k-commit repo still has only ~40 shards per embedder.
- **Query fan-out is time-range-bounded, not history-bounded**: `--time-range-all` fans out across every existing shard (bounded by the small shard count above); a narrow `--time-range` only touches the shards whose quarter overlaps the requested window, so a query for "last quarter" never pays the cost of scanning years of unrelated history. Per-commit aggregation reduces the vectors WITHIN each shard; quarterly sharding reduces which shards a given query must even open.
- **Retention/lifecycle management**: quarterly shard boundaries give golden-repo administrators a natural, low-blast-radius unit for archiving or pruning old temporal data (e.g. dropping shards older than a retention policy) without needing to rewrite or partially edit one single combined index -- a per-commit-only (unsharded) index would require expensive in-place vector deletion instead of a directory delete.
- **HNSW index rebuild cost stays bounded**: each shard's HNSW graph only ever contains that quarter's vectors, so a rebuild (e.g. after a delta reindex) touches a bounded, small graph rather than the full multi-year history's graph.

## Troubleshooting

### No Temporal Results

**Symptom**: Query returns 0 results or "temporal index not found"

**Solutions**:

1. **Verify temporal index exists**:
   ```bash
   ls -lh .code-indexer/index/ | grep code-indexer-temporal
   ```

2. **Index commits if missing**:
   ```bash
   cidx index --index-commits
   ```

3. **Try broader query**:
   ```bash
   # Start with --time-range-all and no filters
   cidx query "anything" --time-range-all --quiet
   ```

### Slow Temporal Queries

**Causes**:
- Very large commit history
- Broad time range
- Complex semantic query

**Solutions**:

```bash
# Narrow time range
cidx query "feature" --time-range 2024-11-01..2024-11-30 --quiet

# Add author filter
cidx query "feature" --time-range-all --author "specific-dev" --quiet

# Use chunk type to narrow scope
cidx query "feature" --time-range-all --chunk-type commit_message --quiet
```

### Temporal Index Outdated

**Symptom**: Recent commits not showing in results

**Solution**:
```bash
# Reindex to include latest commits
cidx index --index-commits

# This incrementally updates the temporal index
```

### Wrong Chunk Type

**Symptom**: Expected results not found

**Check**:
- Use `commit_message` for ticket numbers, keywords in messages
- Use `commit_diff` for actual code changes
- Omit `--chunk-type` to search both

```bash
# Search both messages and diffs (default)
cidx query "OAuth" --time-range-all --quiet

# Search only messages
cidx query "JIRA-123" --time-range-all --chunk-type commit_message --quiet

# Search only code
cidx query "authentication logic" --time-range-all --chunk-type commit_diff --quiet
```

### Memory Issues During Indexing

**Symptom**: Out of memory error during `cidx index --index-commits`

**Solutions**:

1. **Increase available memory** (if possible)

2. **Index in smaller batches** (not currently supported - would need implementation)

3. **Exclude large binary files**:
   ```bash
   # Add to .gitignore before indexing
   echo "*.mp4" >> .gitignore
   echo "*.zip" >> .gitignore
   ```

---

## Next Steps

- **Query Guide**: [Complete Query Reference](query-guide.md)
- **Operating Modes**: [Operating Modes Guide](operating-modes.md)
- **Installation**: [Installation Guide](installation.md)
- **Main Documentation**: [README](../README.md)

---

## Related Documentation

- **Architecture**: [Architecture Guide](architecture.md)
- **SCIP**: [SCIP Code Intelligence](scip/README.md)
- **Configuration**: [Configuration Guide](configuration.md)

---

