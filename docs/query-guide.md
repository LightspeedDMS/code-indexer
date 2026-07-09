# Query Guide

Complete guide to searching code with CIDX across all search modes and query parameters.

## Table of Contents

- [Quick Reference](#quick-reference)
- [Search Modes](#search-modes)
  - [Semantic Search](#semantic-search)
  - [Full-Text Search (FTS)](#full-text-search-fts)
  - [Regex Search](#regex-search)
  - [Hybrid Search](#hybrid-search)
- [Query Parameters](#query-parameters)
- [Filtering](#filtering)
- [Temporal Queries](#temporal-queries)
- [Performance Tuning](#performance-tuning)
- [Best Practices](#best-practices)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)

## Quick Reference

```bash
# Semantic search (default)
cidx query "authentication logic"

# Full-text search
cidx query "authenticate_user" --fts

# Regex search
cidx query "def.*test" --fts --regex

# Hybrid search (semantic + FTS)
cidx query "user authentication" --fts --semantic

# With filtering
cidx query "database" --language python --path-filter "*/models/*"

# Temporal search (git history)
cidx query "JWT auth" --time-range-all --quiet
```

## Search Modes

### Semantic Search

**Default mode** - Finds code by meaning using AI embeddings.

**Use When**:
- Searching by concept or functionality
- Don't know exact symbol names
- Want to find similar implementations
- Exploring unfamiliar codebase

**Examples**:
```bash
# Find authentication code
cidx query "user authentication logic"

# Find database connections
cidx query "how to connect to database"

# Find error handling
cidx query "exception handling patterns"

# Find specific functionality
cidx query "JWT token validation"
```

**How It Works**:
1. Query converted to embedding vector
2. HNSW index searched for similar vectors
3. Results ranked by cosine similarity
4. Min score threshold filters low-confidence matches

**Performance**: ~20ms per query (HNSW index)

### Full-Text Search (FTS)

**Fast exact text matching** - 1.36x faster than grep on indexed codebases.

**Use When**:
- Searching for exact identifiers (function names, variables)
- Know exact text to find
- Need case-sensitive matching
- Want typo tolerance (fuzzy matching)

**Examples**:
```bash
# Find function by name
cidx query "authenticate_user" --fts

# Case-sensitive search
cidx query "ParseError" --fts --case-sensitive

# Fuzzy matching (typo tolerance)
cidx query "authenticte" --fts --fuzzy  # Finds "authenticate"

# With context lines
cidx query "def validate" --fts --snippet-lines 10
```

**How It Works**:
1. Tantivy FTS index (Rust-based)
2. Token-based exact matching
3. Optional fuzzy matching (edit distance)
4. Returns matching files with context

**Performance**: <100ms per query, 1.36x faster than grep

### Regex Search

**Pattern matching** - 10-50x faster than grep for token-based patterns.

**Use When**:
- Searching for patterns (test_*, class.*, def.*())
- Complex string patterns
- Token-level matching

**Examples**:
```bash
# Find function definitions
cidx query "def" --fts --regex

# Find test functions
cidx query "test_.*" --fts --regex --language python

# Find class methods
cidx query "class.*authenticate" --fts --regex

# Find TODO comments
cidx query "TODO|FIXME" --fts --regex
```

**How It Works**:
1. Query interpreted as regex pattern
2. Tantivy applies regex to tokens
3. Token-level matching (not grep-style line matching)
4. Results include matching files

**Performance**: 10-50x faster than grep (token-based)

**Limitations**:
- Token-based (not arbitrary regex like grep)
- Cannot combine with fuzzy matching

### Hybrid Search

**Combine semantic and FTS** - Best of both worlds.

**Use When**:
- Want conceptual matches + exact matches
- Broader search coverage
- Exploring and validating findings

**Examples**:
```bash
# Find auth code semantically + exact "JWT"
cidx query "JWT authentication" --fts --semantic

# Find test code with specific patterns
cidx query "user validation tests" --fts --semantic --regex
```

**How It Works**:
1. Runs both semantic and FTS search
2. Merges results
3. Ranks by combined relevance

**Performance**: Combined time of both modes

## Query Parameters

CIDX supports query parameters across CLI, REST API, and MCP interfaces.

### Core Parameters

| Parameter | CLI Flag | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| **query** | QUERY (positional) | string | required | Search query text |
| **limit** | --limit N | int | 10 | Maximum results (1-100) |
| **min_score** | --min-score N | float | None | Minimum similarity score (0.0-1.0) |

**Examples**:
```bash
cidx query "search text" --limit 20 --min-score 0.7
```

### Language and Path Filtering

| Parameter | CLI Flag | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| **language** | --language LANG | string | None | Filter by programming language |
| **path_filter** | --path-filter PATTERN | string | None | Include files matching glob pattern |
| **exclude_language** | --exclude-language LANG | string | None | Exclude specified language |
| **exclude_path** | --exclude-path PATTERN | string | None | Exclude files matching glob pattern |
| **file_extensions** | --file-extensions EXTS | array | None | Filter by file extensions |

**Supported Languages**:
python, javascript, typescript, java, c, cpp, csharp, go, rust, kotlin, swift, ruby, php, lua, groovy, pascal, sql, html, css, yaml, xml, markdown, and more

**Glob Pattern Syntax**:
- `*` - Match any characters
- `**` - Match any path segments
- `?` - Match single character
- `[seq]` - Match character class

Note: patterns starting with `*/` match at any depth including the repository root.
`*/tests/*` matches both `tests/foo.py` (root) and `src/tests/foo.py` (nested).
`**/tests/**` is equivalent and also accepted.

**Examples**:
```bash
# Filter by language
cidx query "database" --language python

# Path filtering
cidx query "model" --path-filter "*/src/*"

# Exclude tests
cidx query "business logic" --exclude-path "*/tests/*"

# Exclude multiple languages
cidx query "api" --exclude-language javascript --exclude-language css

# Combine filters
cidx query "auth" --language python --path-filter "*/src/*" --exclude-path "*/tests/*"
```

### Search Mode Selection

| Parameter | CLI Flag | Type | Values | Default |
|-----------|----------|------|--------|---------|
| **search_mode** | --fts / --semantic | enum | semantic, fts, hybrid | semantic |

**Examples**:
```bash
# Semantic (default)
cidx query "authentication"

# FTS
cidx query "authenticate_user" --fts

# Hybrid
cidx query "user auth" --fts --semantic
```

### Search Accuracy

| Parameter | CLI Flag | Type | Values | Default |
|-----------|----------|------|--------|---------|
| **accuracy** | --accuracy LEVEL | enum | fast, balanced, high | balanced |

**When to Use**:
- **fast**: Quick results, lower precision
- **balanced**: Good tradeoff (default)
- **high**: Maximum precision, slower

**Examples**:
```bash
cidx query "security vulnerabilities" --accuracy high
```

### FTS-Specific Parameters

| Parameter | CLI Flag | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| **case_sensitive** | --case-sensitive | bool | false | Case-sensitive matching |
| **fuzzy** | --fuzzy | bool | false | Typo tolerance (edit distance 1) |
| **edit_distance** | --edit-distance N | int | 0 | Fuzzy match tolerance (0-3) |
| **snippet_lines** | --snippet-lines N | int | 5 | Context lines around matches (0-50) |
| **regex** | --regex | bool | false | Interpret query as regex pattern |

**Constraints**:
- FTS parameters only work with `--fts` or hybrid mode
- `--regex` and `--fuzzy` are mutually exclusive

**Examples**:
```bash
# Case-sensitive search
cidx query "ParseError" --fts --case-sensitive

# Fuzzy matching (typo tolerance)
cidx query "authenticte" --fts --fuzzy

# Custom edit distance
cidx query "databse" --fts --edit-distance 2

# More context lines
cidx query "validate_user" --fts --snippet-lines 15

# Regex search
cidx query "test_.*_auth" --fts --regex
```

### Temporal Query Parameters

Search git history semantically.

| Parameter | CLI Flag | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| **time_range** | --time-range RANGE | string | None | Time range (YYYY-MM-DD..YYYY-MM-DD) |
| **time_range_all** | --time-range-all | flag | false | Search all git history |
| **diff_type** | --diff-type TYPE | string | None | Filter by diff type |
| **author** | --author NAME | string | None | Filter by commit author |
| **chunk_type** | --chunk-type TYPE | enum | None | commit_message or commit_diff |

**API-Only Temporal Parameters** (not exposed in CLI):
- **at_commit**: Query code at specific commit hash
- **include_removed**: Include removed files
- **show_evolution**: Show code evolution timeline
- **evolution_limit**: Limit evolution entries

**Requirements**:
- Must index commits first: `cidx index --index-commits`

**Examples**:
```bash
# Index commits first
cidx index --index-commits

# Search all history
cidx query "authentication refactor" --time-range-all --quiet

# Specific time range
cidx query "bug fix" --time-range 2024-01-01..2024-12-31 --quiet

# Filter by author
cidx query "login feature" --time-range-all --author "john@example.com" --quiet

# Search only commit messages
cidx query "JIRA-123" --time-range-all --chunk-type commit_message --quiet

# Filter by diff type
cidx query "auth" --time-range-all --diff-type added --quiet
```

**Diff Types**:
- `added` - Newly added code
- `modified` - Changed code
- `deleted` - Removed code
- `renamed` - Renamed files
- `binary` - Binary file changes

## Filtering

### Language Filtering

```bash
# Include specific language
cidx query "model" --language python

# Exclude language
cidx query "api" --exclude-language javascript
```

### Path Filtering

```bash
# Include path pattern
cidx query "auth" --path-filter "*/src/auth/*"

# Exclude path pattern
cidx query "core logic" --exclude-path "*/tests/*" --exclude-path "*/docs/*"

# Combine with language
cidx query "database" --language python --path-filter "*/models/*"
```

### Multiple Filters

```bash
# Complex filtering
cidx query "user management" \
  --language python \
  --path-filter "*/src/*" \
  --exclude-path "*/tests/*" \
  --exclude-language javascript \
  --min-score 0.8 \
  --limit 20
```

## Temporal Queries

### Setup

```bash
# Index git history (one-time setup)
cidx index --index-commits

# Verify temporal index exists
ls -lh .code-indexer/index/*/temporal_meta.json
```

### Basic Temporal Search

```bash
# Search all git history
cidx query "JWT authentication" --time-range-all --quiet

# Always use --quiet for temporal queries (cleaner output)
```

### Time Range Filtering

```bash
# Specific date range
cidx query "auth refactor" --time-range 2024-01-01..2024-06-30 --quiet

# Last year
cidx query "security fix" --time-range 2024-01-01..2024-12-31 --quiet

# Specific month
cidx query "login update" --time-range 2024-03-01..2024-03-31 --quiet
```

### Author Filtering

```bash
# Filter by author email
cidx query "feature implementation" --time-range-all --author "dev@example.com" --quiet

# Filter by author name (partial match)
cidx query "refactoring" --time-range-all --author "John" --quiet
```

### Chunk Type Filtering

```bash
# Search only commit messages
cidx query "JIRA-123" --time-range-all --chunk-type commit_message --quiet

# Search only code diffs
cidx query "password validation" --time-range-all --chunk-type commit_diff --quiet
```

### Diff Type Filtering

```bash
# Find when code was added
cidx query "JWT validation" --time-range-all --diff-type added --quiet

# Find what was deleted
cidx query "legacy auth" --time-range-all --diff-type deleted --quiet

# Find modified code
cidx query "security update" --time-range-all --diff-type modified --quiet
```

### Combined Temporal Filters

```bash
# Complex temporal query
cidx query "authentication changes" \
  --time-range 2024-01-01..2024-12-31 \
  --author "security-team@example.com" \
  --chunk-type commit_diff \
  --diff-type modified \
  --language python \
  --quiet
```

## Performance Tuning

### Start Small

```bash
# Start with low limit for quick results
cidx query "search term" --limit 5

# Increase if needed
cidx query "search term" --limit 20
```

### Use Accuracy Wisely

```bash
# Fast exploration
cidx query "concept" --accuracy fast --limit 10

# Balanced (default) for most use cases
cidx query "concept" --accuracy balanced

# High accuracy for critical searches
cidx query "security vulnerability" --accuracy high
```

### Filter Aggressively

```bash
# Narrow scope with filters (faster queries)
cidx query "model" --language python --path-filter "*/core/*"

# Broad scope (slower)
cidx query "model"  # Searches everything
```

### Choose Right Search Mode

| Mode | Speed | Use Case |
|------|-------|----------|
| Semantic | ~20ms | Conceptual search |
| FTS | <100ms | Exact text search |
| Regex | <100ms | Pattern matching |
| Hybrid | ~120ms | Combined search |

## Best Practices

### 1. Choose Appropriate Search Mode

```bash
# Concept → Semantic
cidx query "user authentication workflow"

# Exact identifier → FTS
cidx query "validate_user_credentials" --fts

# Pattern → Regex
cidx query "test_.*_auth" --fts --regex
```

### 2. Start Broad, Refine Narrow

```bash
# Step 1: Broad search
cidx query "authentication" --limit 10

# Step 2: Refine with filters
cidx query "authentication" --language python --path-filter "*/auth/*" --limit 5
```

### 3. Use Min Score Effectively

```bash
# High confidence matches only
cidx query "security vulnerability" --min-score 0.8

# Cast wider net
cidx query "helper functions" --min-score 0.5
```

### 4. Combine Modes for Validation

```bash
# Find conceptually, validate exactly
cidx query "JWT validation" --fts --semantic --limit 15
```

### 5. Temporal Search for Code Archaeology

```bash
# When was feature added?
cidx query "OAuth integration" --time-range-all --diff-type added --quiet

# Who worked on auth?
cidx query "authentication" --time-range-all --author "security" --quiet

# What changed recently?
cidx query "login" --time-range 2024-11-01..2024-12-31 --diff-type modified --quiet
```

## Examples

### Find API Endpoints

```bash
# Semantic
cidx query "REST API endpoints" --limit 10

# FTS for exact route definitions
cidx query "@app.route" --fts --language python
```

### Find Test Files

```bash
# Pattern matching
cidx query "test_.*" --fts --regex --path-filter "*/tests/*"

# Semantic
cidx query "unit tests for authentication" --language python
```

### Find Database Models

```bash
# Semantic
cidx query "database models" --language python --path-filter "*/models/*"

# FTS for class names
cidx query "class.*Model" --fts --regex --language python
```

### Find Security Vulnerabilities

```bash
# High accuracy semantic search
cidx query "SQL injection vulnerability" --accuracy high --min-score 0.8

# Historical security fixes
cidx query "security patch" --time-range-all --chunk-type commit_message --quiet
```

### Find Configuration Files

```bash
# Semantic
cidx query "application configuration settings"

# FTS exact
cidx query "config.yaml" --fts

# By extension (API)
# Use REST/MCP API with file_extensions parameter
```

### Find Error Handling

```bash
# Semantic
cidx query "exception handling patterns"

# FTS for try/catch blocks
cidx query "try:.*except" --fts --regex --language python
```

## Troubleshooting

### No Results Found

**Possible Causes**:
1. Query too specific
2. Min score too high
3. Aggressive filtering
4. Code not indexed

**Solutions**:
```bash
# Broaden query
cidx query "auth" --limit 20 --min-score 0.5

# Remove filters
cidx query "authentication"  # No language/path filters

# Reindex
cidx index --clear
cidx index
```

### Too Many Results

**Solutions**:
```bash
# Increase min score
cidx query "function" --min-score 0.8

# Add filters
cidx query "function" --language python --path-filter "*/core/*"

# Use exact search
cidx query "specific_function_name" --fts
```

### Slow Queries

**Possible Causes**:
1. Large codebase
2. High limit value
3. Complex regex
4. Temporal queries without filters

**Solutions**:
```bash
# Reduce limit
cidx query "search" --limit 5

# Add filters to narrow scope
cidx query "search" --language python

# Use fast accuracy
cidx query "search" --accuracy fast

# For temporal, always use --quiet
cidx query "search" --time-range-all --quiet
```

### Fuzzy Matching Not Working

**Check**:
```bash
# Fuzzy requires --fts mode
cidx query "authenticte" --fts --fuzzy

# Not this (wrong - no --fts)
cidx query "authenticte" --fuzzy  # Won't work
```

### Regex Not Matching

**Common Issues**:
1. Token-based matching (not line-based like grep)
2. Need --fts --regex flags
3. Pattern syntax

**Examples**:
```bash
# Correct: Token-based pattern
cidx query "def" --fts --regex

# Wrong: Line-based pattern (use grep instead)
# cidx does token-based, not arbitrary regex
```

### Temporal Queries Return Nothing

**Check**:
```bash
# 1. Verify commits were indexed
ls -lh .code-indexer/index/*/temporal_chunks.json

# 2. If missing, index commits
cidx index --index-commits

# 3. Verify with --time-range-all
cidx query "anything" --time-range-all --quiet
```

---

## Next Steps

- **Installation**: [Installation Guide](installation.md)
- **SCIP Code Intelligence**: [SCIP Guide](scip/README.md)
- **Operating Modes**: [Operating Modes](operating-modes.md)
- **Main Documentation**: [README](../README.md)

## Parameter Reference

Complete list of CLI query parameters with flags, types, and defaults.

| Parameter | CLI Flag | Type | Default | Modes | Description |
|-----------|----------|------|---------|-------|-------------|
| query | QUERY | string | required | All | Search query text |
| limit | --limit | int | 10 | All | Max results (1-100) |
| min_score | --min-score | float | None | All | Minimum similarity score (0.0-1.0) |
| language | --language | string (multiple) | None | All | Filter by programming language |
| path_filter | --path-filter | string (multiple) | None | All | Include files matching glob pattern |
| exclude_language | --exclude-language | string (multiple) | None | All | Exclude language |
| exclude_path | --exclude-path | string (multiple) | None | All | Exclude path pattern |
| file_extensions | --file-extensions | string | None | All | Filter by extensions (comma-separated, e.g. "py,js,ts") |
| search_mode | --fts / --semantic | enum | semantic | All | semantic/fts/hybrid |
| accuracy | --accuracy | enum | balanced | All | fast/balanced/high |
| case_sensitive | --case-sensitive | bool | false | FTS | Case-sensitive match |
| case_insensitive | --case-insensitive | bool | false | FTS | Case-insensitive match |
| fuzzy | --fuzzy | bool | false | FTS | Typo tolerance |
| edit_distance | --edit-distance | int | 0 | FTS | Fuzzy tolerance (0-3) |
| snippet_lines | --snippet-lines | int | 5 | FTS | Context lines (0-50) |
| regex | --regex | bool | false | FTS | Regex pattern |
| rerank_query | --rerank-query | string | None | All | Reranker query (requires API key) |
| rerank_instruction | --rerank-instruction | string | None | All | Reranker instruction hint |
| time_range | --time-range | string | None | Temporal | Date range filter |
| diff_type | --diff-type | string (multiple) | None | Temporal | Diff type filter (can be specified multiple times) |
| author | --author | string | None | Temporal | Author filter |
| chunk_type | --chunk-type | enum | None | Temporal | commit_message/commit_diff |
| repo | --repo | string | None | Remote | Query global repo by alias |
| repos | --repos | string | None | Remote | Query multiple repos (comma-separated) |

**API-Only Parameters** (not available as CLI flags):
- at_commit (string) - Query at specific commit
- include_removed (bool) - Include removed files
- show_evolution (bool) - Show code evolution
- evolution_limit (int) - Limit evolution entries
