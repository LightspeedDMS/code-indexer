FACT-CHECKED

Last Fact-Check: 2025-12-31
Verified Against: CIDX source code (src/code_indexer/)

# Configuration Guide

Complete reference for configuring CIDX.

## Table of Contents

- [Overview](#overview)
- [Embedding Provider](#embedding-provider)
- [Configuration File](#configuration-file)
- [Environment Variables](#environment-variables)
- [Per-Project vs Global](#per-project-vs-global)
- [Advanced Configuration](#advanced-configuration)
- [Troubleshooting](#troubleshooting)

## Overview

CIDX configuration involves three main areas:

1. **Embedding Provider** - VoyageAI or Cohere API key (required for semantic search)
2. **Configuration File** - `.code-indexer/config.json` per project
3. **Environment Variables** - Optional tuning and server settings

## Embedding Provider

CIDX supports multiple embedding providers as of v9.8.0. At least one provider must be configured for semantic search. When multiple providers are configured, you can use multi-provider query strategies (see [Query Guide -- Multi-Provider Query Strategy](query-guide.md#multi-provider-query-strategy)).

### VoyageAI

CIDX uses **VoyageAI embeddings** for semantic search. This is the default provider.

**Get API Key**:
1. Sign up at https://www.voyageai.com/
2. Navigate to API Keys section
3. Generate new API key
4. Copy your API key

### Configure API Key

**Option 1: Environment Variable (Recommended)**

```bash
# Add to shell profile (~/.bashrc, ~/.zshrc, etc.)
export VOYAGE_API_KEY="your-api-key-here"

# Reload shell
source ~/.bashrc  # or ~/.zshrc
```

**Option 2: .env.local File (Manual Loading)**

```bash
# Create .env.local in project directory
echo 'VOYAGE_API_KEY=your-api-key-here' > .env.local

# Load into shell environment (one-time per session)
export $(cat .env.local | xargs)
```

**Important**: CIDX does NOT automatically load `.env.local` files. You must export environment variables to your shell before running `cidx` commands. The `.env.local` file is simply a convenient place to store the key - you need to load it manually using `export` or similar shell commands.

**Option 3: Per-Session**

```bash
# Set for current session only
export VOYAGE_API_KEY="your-api-key-here"

# Run CIDX commands
cidx query "search term"
```

### Verify Setup

```bash
# Check environment variable
echo $VOYAGE_API_KEY

# Should output your API key
```

### Supported Models

CIDX supports multiple VoyageAI models:

| Model | Dimensions | Use Case |
|-------|------------|----------|
| **voyage-code-3** | 1024 | Default, optimized for code |
| **voyage-3-large** | 1024 | State-of-the-art general-purpose model |
| **voyage-large-2-instruct** | 1536 | Instruction-tuned large model |

**Model Selection**: User-selectable via `--voyage-model` flag during `cidx init`. Default is `voyage-code-3`.

### Cohere

Cohere is supported as an embedding provider starting in v9.8.0. It can be used as the primary provider, as a secondary alongside VoyageAI, or as a standalone provider.

**Get API Key**:
1. Sign up at https://dashboard.cohere.com/
2. Navigate to API Keys
3. Generate a production API key
4. Copy your API key

**Configure API Key**:

**Option 1: Environment Variable**

```bash
# Add to shell profile (~/.bashrc, ~/.zshrc, etc.)
export CO_API_KEY="your-cohere-api-key-here"

# Reload shell
source ~/.bashrc  # or ~/.zshrc
```

**Option 2: Configuration File**

Set the API key directly in `.code-indexer/config.json` (see Configuration File section below).

**Verify Setup**:

```bash
# Check environment variable
echo $CO_API_KEY

# Check provider health
cidx provider-health
```

**Supported Models**:

| Model | Description |
|-------|-------------|
| **embed-v4.0** | Default Cohere embedding model |

**Configuration in config.json**:

To use Cohere as the embedding provider, set `embedding_provider` to `"cohere"` and add a `cohere` configuration block:

```json
{
  "embedding_provider": "cohere",
  "cohere": {
    "api_key": "your-cohere-key",
    "model": "embed-v4.0",
    "max_retries": 3
  }
}
```

If `CO_API_KEY` is set in the environment, the `api_key` field in config.json can be omitted. The environment variable takes precedence.

### Provider Health Monitoring

When one or more providers are configured, use `cidx provider-health` to check the status, success rates, and latency of each provider:

```bash
cidx provider-health
```

This reports per-provider metrics including availability, request success rate, and average response latency.

### Multi-Provider Setup

To configure both VoyageAI and Cohere simultaneously, set both API keys (via environment variables or config) and specify the primary provider in `embedding_provider`. The secondary provider is available for failover and parallel query strategies.

```json
{
  "embedding_provider": "voyage-ai",
  "cohere": {
    "model": "embed-v4.0",
    "max_retries": 3
  }
}
```

With both `VOYAGE_API_KEY` and `CO_API_KEY` set in the environment, this configuration uses VoyageAI as the primary provider and Cohere as the secondary. Query strategy flags (`--strategy failover`, `--strategy parallel`) control how the secondary provider is used at query time.

## Configuration File

### Location

**Per-Project**: `.code-indexer/config.json` in each indexed project

Created automatically by `cidx init` or first `cidx index`.

### Structure

```json
{
  "file_extensions": [
    "py", "js", "ts", "tsx", "java", "cpp", "c", "cs", "h", "hpp",
    "go", "rs", "rb", "php", "pl", "pm", "pod", "t", "psgi",
    "sh", "bash", "html", "css", "md", "json", "yaml", "yml", "toml",
    "sql", "swift", "kt", "kts", "scala", "dart", "vue", "jsx",
    "pas", "pp", "dpr", "dpk", "inc", "lua", "xml", "xsd", "xsl",
    "xslt", "groovy", "gradle", "gvy", "gy", "cxx", "cc", "hxx",
    "rake", "rbw", "gemspec", "htm", "scss", "sass"
  ],
  "exclude_dirs": [
    "node_modules", "venv", "__pycache__", ".git", "dist", "build",
    "target", ".idea", ".vscode", ".gradle", "bin", "obj",
    "coverage", ".next", ".nuxt", "dist-*", ".code-indexer"
  ],
  "embedding_provider": "voyage-ai",
  "indexing": {
    "max_file_size": 1048576
  }
}
```

### Configuration Fields

#### file_extensions

**Type**: Array of strings (WITHOUT dot prefix)
**Default**: Common code file extensions (see structure above)
**Purpose**: File types to index

**Important**: Extensions are specified WITHOUT dots (e.g., "py" not ".py"). The system adds dots automatically.

**Customization**:
```json
{
  "file_extensions": [
    "py", "js", "ts"  // Only Python and JavaScript/TypeScript
  ]
}
```

**Add More Extensions**:
```json
{
  "file_extensions": [
    "py", "js", "ts",
    "jsx", "tsx",    // React
    "vue",            // Vue
    "svelte",         // Svelte
    "scala",          // Scala
    "dart"            // Dart
  ]
}
```

#### exclude_dirs

**Type**: Array of strings
**Default**: See complete list in Structure section above
**Purpose**: Directories to exclude from indexing

**Default List Includes**:
- Build outputs: node_modules, dist, build, target, bin, obj
- Version control: .git
- Virtual environments: venv, __pycache__
- IDE configs: .idea, .vscode, .gradle
- Test artifacts: coverage, .pytest_cache
- Framework outputs: .next, .nuxt
- CIDX internal: .code-indexer

**Customization**:
```json
{
  "exclude_dirs": [
    "node_modules", ".git", "__pycache__",
    "vendor",           // Add PHP vendor
    "Pods",             // Add iOS Pods
    "build-output"      // Custom build dir
  ]
}
```

**Include More**:
```json
{
  "exclude_dirs": [
    "node_modules", ".git",
    "test_data",        // Test fixtures
    "mock_apis",        // Mock data
    "legacy_code"       // Deprecated code
  ]
}
```

#### embedding_provider

**Type**: String
**Default**: "voyage-ai"
**Purpose**: Embedding provider selection

**Supported values**:
- `"voyage-ai"` -- VoyageAI (default)
- `"cohere"` -- Cohere (v9.8.0+)

#### max_file_size

**Type**: Integer (bytes)
**Default**: 1048576 (1 MB)
**Purpose**: Maximum file size to index
**Location**: Nested under "indexing" object in config.json

**Customization**:
```json
{
  "indexing": {
    "max_file_size": 2097152  // 2 MB
  }
}
```

**Why Limit File Size?**:
- Large files increase indexing time
- Embedding API has token limits
- Quality degrades for very large files

**Recommendations**:
- **1 MB (default)**: Good for most code files
- **2-5 MB**: If you have larger source files
- **<500 KB**: If you want faster indexing

### Manual Editing

You can manually edit `.code-indexer/config.json`:

```bash
# Edit config
nano .code-indexer/config.json

# Reindex to apply changes
cidx index --clear
cidx index
```

**Important**: Changes take effect after re-indexing.

## Environment Variables

### Required

At least one embedding provider API key must be set.

| Variable | Purpose | Example |
|----------|---------|---------|
| **VOYAGE_API_KEY** | VoyageAI API key | `export VOYAGE_API_KEY="your-api-key"` |
| **CO_API_KEY** | Cohere API key (v9.8.0+) | `export CO_API_KEY="your-cohere-key"` |

### Optional (Server Mode Only)

**Note**: These variables are ONLY used when running CIDX in server mode (multi-user deployment). They are NOT used in CLI or Daemon modes.

| Variable | Purpose | Default | Example |
|----------|---------|---------|---------|
| **CIDX_INDEX_CACHE_TTL_MINUTES** | Server cache TTL | 10 | `export CIDX_INDEX_CACHE_TTL_MINUTES=30` |
| **CIDX_SERVER_PORT** | Server port | 8000 | `export CIDX_SERVER_PORT=9000` |
| **CIDX_SERVER_HOST** | Server host | localhost | `export CIDX_SERVER_HOST=0.0.0.0` |

For CLI/Daemon mode configuration, use `cidx config` commands instead (see Daemon Mode section).

### Setting Environment Variables

**Linux/macOS**:
```bash
# Temporary (current session)
export VOYAGE_API_KEY="your-key"

# Permanent (add to ~/.bashrc or ~/.zshrc)
echo 'export VOYAGE_API_KEY="your-key"' >> ~/.bashrc
source ~/.bashrc
```

**Windows (PowerShell)**:
```powershell
# Temporary (current session)
$env:VOYAGE_API_KEY = "your-key"

# Permanent (System Environment Variables)
# Control Panel → System → Advanced → Environment Variables
```

## Per-Project vs Global

### Per-Project Configuration

**Location**: `.code-indexer/` in each project
**Scope**: Single project only
**Use Case**: Project-specific settings

**Files**:
- `config.json` - Configuration
- `index/` - Vector indexes
- `scip/` - SCIP indexes

**Setup**:
```bash
cd /path/to/project
cidx index
# Creates .code-indexer/ in current directory
```

### Global Registry (Deprecated)

**Note**: The global registry (`~/.code-indexer/registry.json`) is **deprecated** since v8.0. CIDX no longer requires centralized registry for CLI/Daemon modes.

**Server Mode**: Still uses `~/.cidx-server/data/` for golden repositories, but this is server-specific, not a global registry.

## Advanced Configuration

### Daemon Mode

```bash
# Enable daemon mode
cidx config --daemon

# Disable daemon mode
cidx config --no-daemon

# Check current mode
cidx status
```

**What It Configures**:
- Enables background daemon process
- Activates in-memory caching
- Enables watch mode capability

### Watch Mode

```bash
# Start watch mode (requires daemon)
cidx watch

# Custom debounce
cidx watch --debounce 3.0

# With FTS indexing
cidx watch --fts
```

**Watch Mode Settings**:
- **Debounce**: Default 2.0 seconds (configurable via `--debounce`)
- **File monitoring**: Watches all files matching `file_extensions`
- **Excludes**: Respects `exclude_dirs` from config.json

### Language-Specific Indexing

Customize which languages to index by editing `file_extensions`:

```json
{
  "file_extensions": [
    ".py"              // Python only
  ]
}
```

Or use `--language` flag during queries:
```bash
cidx query "search" --language python
```

### Index Type Selection

```bash
# Semantic only (default)
cidx index

# Add full-text search
cidx index --fts

# Add git history
cidx index --index-commits

# All index types
cidx index --fts --index-commits

# SCIP code intelligence
cidx scip generate
```

## Troubleshooting

### API Key Not Found

**Error**: `ERROR: VOYAGE_API_KEY environment variable not set`

**Solutions**:

1. **Set environment variable**:
   ```bash
   export VOYAGE_API_KEY="your-key"
   ```

2. **Add to shell profile**:
   ```bash
   echo 'export VOYAGE_API_KEY="your-key"' >> ~/.bashrc
   source ~/.bashrc
   ```

3. **Verify it's set**:
   ```bash
   echo $VOYAGE_API_KEY
   ```

### Config File Corrupted

**Error**: `ERROR: Invalid config.json`

**Solutions**:

1. **Delete and recreate**:
   ```bash
   rm .code-indexer/config.json
   cidx index
   # Creates fresh config.json with defaults
   ```

2. **Manually fix JSON**:
   ```bash
   nano .code-indexer/config.json
   # Fix JSON syntax errors
   ```

3. **Validate JSON**:
   ```bash
   python3 -m json.tool .code-indexer/config.json
   # Shows JSON syntax errors
   ```

### File Size Limit Too Restrictive

**Symptom**: Large files not indexed

**Solution**:
```json
{
  "max_file_size": 5242880  // Increase to 5 MB
}
```

Then reindex:
```bash
cidx index --clear
cidx index
```

### Excluded Directory Needed

**Symptom**: Important code in excluded directory not indexed

**Solution**:

1. **Edit config.json**:
   ```json
   {
     "exclude_dirs": [
       "node_modules", ".git"  // Removed "__pycache__"
     ]
   }
   ```

2. **Reindex**:
   ```bash
   cidx index --clear
   cidx index
   ```

### Wrong File Extensions

**Symptom**: Code files not being indexed

**Solution**:

1. **Add extensions to config.json**:
   ```json
   {
     "file_extensions": [
       ".py", ".js", ".ts",
       ".jsx", ".tsx"  // Add React extensions
     ]
   }
   ```

2. **Reindex**:
   ```bash
   cidx index
   ```

### Daemon Configuration Issues

**Problem**: Daemon mode not persisting

**Check**:
```bash
# Verify daemon mode enabled
cidx status

# Re-enable if needed
cidx config --daemon
cidx start
```

### Cohere API Key Not Found

**Error**: `ERROR: CO_API_KEY environment variable not set`

**Solutions**:

1. **Set environment variable**:
   ```bash
   export CO_API_KEY="your-cohere-key"
   ```

2. **Or set in config.json**:
   ```json
   {
     "embedding_provider": "cohere",
     "cohere": {
       "api_key": "your-cohere-key"
     }
   }
   ```

3. **Verify it's set**:
   ```bash
   echo $CO_API_KEY
   ```

### Provider Health Check Failing

**Problem**: `cidx provider-health` shows errors or high latency for a provider.

**Solutions**:

1. **Verify API key is valid** for the failing provider.
2. **Check provider status page** (VoyageAI or Cohere) for outages.
3. **Use failover strategy** to automatically route around a failing provider:
   ```bash
   cidx query "search term" --strategy failover
   ```

---

## Runtime Settings (v9.x to v10.0)

These settings are runtime-configurable via the Web UI Config Screen and persist via the runtime DB (SQLite solo / PostgreSQL cluster). Bootstrap-only settings (those that must live in `config.json` because they are needed before the DB is available) are noted explicitly.

### Research Assistant

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `ra_curl_allowed_cidrs` | List[str] | `[]` | Operator-configured CIDR allowlist for the `cidx-curl.sh` wrapper. Loopback (`127.0.0.0/8` + `::1/128`) is always appended automatically -- operators cannot disable loopback. Empty list = loopback only. Examples: `["10.5.0.0/24"]`, `["10.5.0.0/24", "192.168.100.0/24"]`. **Bootstrap-only** (`config.json` under `claude_integration_config`); restart cidx-server after change. |

### Dep-Map Auto-Repair (Story #927)

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `dep_map_auto_repair_enabled` | bool | `false` | When true, scheduled delta and refinement jobs automatically run a repair pass once if anomalies are detected. Anomalies that don't resolve are retried on the next scheduled cycle. Operator opts in via Web UI. |

### Memory Retrieval (Story #883)

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `memory_retrieval_enabled` | bool | `true` | Kill switch for the memory retrieval pipeline. When false, `search_code` does NOT make a VoyageAI call for memory candidates and the `relevant_memories` field is absent from the response. Effective immediately, no restart required. |

### TOTP Step-Up Elevation (Epic #922)

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `elevation_enforcement_enabled` | bool | `false` | When true, admin endpoints requiring elevation reject without an active elevation window (HTTP 403 `elevation_required`). When false, all elevation checks no-op (HTTP 503 -- feature administratively off). Hot-reload via 30s reload thread; no restart required. |

### Server Memory Mitigations (Bug #897)

These are bootstrap-only flags in `config.json` (defaults ON since v9.23.3):

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `enable_malloc_trim` | bool | `true` | Calls glibc `malloc_trim(0)` at end of each HNSW cache cleanup cycle. Linux+glibc only; silently no-ops elsewhere. |
| `enable_malloc_arena_max` | bool | `true` | Idempotently injects `MALLOC_ARENA_MAX=2` into cidx-server systemd unit file via auto-updater. |
| `enable_graph_channel_repair` | bool | `true` | Phase 3.7 dep-map graph-channel repair (bootstrap-only). When false, `_run_phase37` returns immediately. |

### Omni Search Caps (Bug #881, Bug #894)

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `omni_wildcard_expansion_cap` | int | `50` | Per-pattern wildcard expansion cap inside `_expand_wildcard_patterns`. |
| `omni_max_repos_per_search` | int | `50` | Total alias fan-out cap after wildcard expansion + literal union. |
| `index_cache_max_size_mb` | int | `4096` | HNSW cache size cap. |
| `fts_cache_max_size_mb` | int | `4096` | FTS cache size cap. |

### Codex CLI Integration (Epic #843)

Bootstrap-only in `config.json`:

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `enable_codex_cli` | bool | `true` | Auto-install Codex CLI via npm at auto-updater run. Optional-feature semantics: missing npm logs WARNING but doesn't abort. |

### Fault Injection Harness (Bug #864)

Bootstrap-only in `config.json` (NEVER enable in production):

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `fault_injection_enabled` | bool | `false` | Master switch. |
| `fault_injection_nonprod_ack` | bool | `false` | Operator acknowledgement that this is a non-production environment. Required alongside `fault_injection_enabled`. |
