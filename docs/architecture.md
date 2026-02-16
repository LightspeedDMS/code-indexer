# Code Indexer Architecture (v8.0+)

This document describes the high-level architecture and design decisions for Code Indexer (CIDX) version 8.0 and later.

## Version 8.0 Architectural Changes

Version 8.0 represents a major architectural simplification:
- **Removed**: Qdrant backend, container infrastructure, Ollama embeddings
- **Consolidated**: Filesystem-only backend, VoyageAI-only embeddings
- **Simplified**: Two operational modes (was three in v7.x)
- **Result**: Container-free, instant setup, reduced complexity

See [Migration Guide](migration-to-v8.md) for upgrading from v7.x.

## Operating Modes

CIDX has **two operational modes** (simplified from three in v7.x), each optimized for different use cases.

### Mode 1: CLI Mode (Direct, Local)

**Purpose**: Direct command-line tool for local semantic code search

**Storage**: FilesystemVectorStore in `.code-indexer/index/` (container-free)

**Use Case**: Individual developers, single-user workflows

**Characteristics**:
- Indexes code locally in project directory
- No daemon, no server, no network
- Vectors stored as JSON files on filesystem
- Each query loads indexes from disk
- Container-free, instant setup

### Mode 2: Daemon Mode (Local, Cached)

**Purpose**: Local RPyC-based background service for faster queries

**Storage**: Same FilesystemVectorStore + in-memory cache

**Use Case**: Developers wanting faster repeated queries and watch mode

**Characteristics**:
- Caches HNSW/FTS indexes in memory (daemon process)
- Auto-starts on first query when enabled
- Unix socket communication (`.code-indexer/daemon.sock`)
- Faster queries (~5ms cached vs ~1s from disk)
- Watch mode for real-time file change indexing
- Container-free, runs as local process

## Vector Storage Architecture (v7.0+)

### HNSW Graph-Based Indexing

Code Indexer v7.0 introduced **HNSW (Hierarchical Navigable Small World)** graph-based indexing for blazing-fast semantic search with **O(log N)** complexity.

**Performance:**
- **300x speedup**: ~20ms queries (vs 6+ seconds with binary index)
- **Scalability**: Tested with 37K vectors, sub-30ms response times
- **Memory efficient**: 154 MB index for 37K vectors (4.2 KB per vector)

**Algorithm Complexity:**
```
Query Time Complexity: O(log N + K)
  - HNSW graph search: O(log N) average case
  - Candidate loading: O(K) where K = limit * 2, K << N
  - Practical: ~20ms for 37K vectors
```

**HNSW Configuration:**
- **M=16**: Connections per node (graph connectivity)
- **ef_construction=200**: Build-time accuracy parameter
- **ef_query=50**: Query-time accuracy parameter
- **Space=cosine**: Cosine similarity distance metric

### Filesystem Vector Store

Container-free vector storage using filesystem + HNSW indexing:

**Storage Structure:**
```
.code-indexer/index/<collection>/
├── hnsw_index.bin              # HNSW graph (O(log N) search)
├── id_index.bin                # Binary mmap ID→path mapping
├── collection_meta.json        # Metadata + staleness tracking
└── vectors/                    # Quantized path structure
    └── <level1>/<level2>/<level3>/<level4>/
        └── vector_<uuid>.json  # Individual vector + payload
```

**Collection Names** (v8.8+):
- `voyage-code-3`: Source code and plain text (default, always present)
- `voyage-multimodal-3`: Markdown with embedded images (created when multimodal content exists)

See [Dual Model Architecture](#dual-model-architecture-v88) for details on multi-collection storage.

**Key Features:**
- **Path-as-Vector Quantization**: 64-dim projection → 4-level directory depth
- **Git-Aware Storage**:
  - Clean files: Store only git blob hash (space efficient)
  - Dirty/non-git: Store full chunk_text
- **Hash-Based Staleness**: SHA256 for precise change detection
- **3-Tier Content Retrieval**: Current file → git blob → error

**Binary ID Index:**
- **Format**: Packed binary `[num_entries:uint32][id_len:uint16, id:utf8, path_len:uint16, path:utf8]...`
- **Performance**: <20ms cached loads via memory mapping (mmap)
- **Thread-safe**: RLock for concurrent access

### Parallel Query Execution

**2-Thread Architecture for 15-30% Latency Reduction:**

```
Query Pipeline:
┌─────────────────────────────────────────┐
│  Thread 1: Index Loading (I/O bound)   │
│  - Load HNSW graph (~5-15ms)           │
│  - Load ID index via mmap (<20ms)      │
└─────────────────────────────────────────┘
           ⬇ Parallel Execution ⬇
┌─────────────────────────────────────────┐
│ Thread 2: Embedding (CPU/Network bound)│
│  - Generate query embedding (5s API)   │
└─────────────────────────────────────────┘
           ⬇ Join ⬇
┌─────────────────────────────────────────┐
│  HNSW Graph Search + Filtering         │
│  - Navigate graph: O(log N)            │
│  - Load K candidates: O(K)             │
│  - Apply filters and score             │
│  - Return top-K results                │
└─────────────────────────────────────────┘
```

**Typical Savings:** 175-265ms per query
**Threading Overhead:** 7-16% (transparently reported)

## Dual Model Architecture (v8.8+)

CIDX v8.8 introduces **multimodal indexing** for markdown files with embedded images, using a dual-model architecture that maintains separate collections for code and multimodal content.

### Model Selection

**voyage-code-3** (Code Collection):
- **Purpose**: Source code, configuration files, plain text documentation
- **Dimensions**: 1024
- **Strengths**: Optimized for code semantics, function/class relationships, programming patterns
- **Collection**: `.code-indexer/index/voyage-code-3/`

**voyage-multimodal-3** (Multimodal Collection):
- **Purpose**: Markdown files containing embedded images (diagrams, screenshots, schemas)
- **Dimensions**: 1024
- **Strengths**: Combined text+image understanding, visual content semantics
- **Collection**: `.code-indexer/index/voyage-multimodal-3/`

### Storage Structure (Dual Collections)

```
.code-indexer/
├── config.json                           # Project configuration
└── index/
    ├── voyage-code-3/                    # Code collection (always present)
    │   ├── hnsw_index.bin
    │   ├── id_index.bin
    │   ├── collection_meta.json
    │   └── vectors/
    │       └── <quantized-path>/vector_<uuid>.json
    │
    └── voyage-multimodal-3/              # Multimodal collection (when images exist)
        ├── hnsw_index.bin
        ├── id_index.bin
        ├── collection_meta.json
        └── vectors/
            └── <quantized-path>/vector_<uuid>.json
```

### Indexing Pipeline

**Automatic Detection**: During `cidx index`, each file is analyzed:

```
File Processing:
┌─────────────────────────────────────────┐
│  1. File Discovery                      │
│     - Scan codebase for indexable files │
└─────────────────────────────────────────┘
           ⬇
┌─────────────────────────────────────────┐
│  2. Content Analysis                    │
│     - Is it markdown (.md)?             │
│       → Parse ![alt](path) syntax       │
│     - Is it HTML/HTMX (.html, .htmx)?   │
│       → Parse <img src="path"> tags     │
│     - Contains image references?        │
└─────────────────────────────────────────┘
           ⬇
┌─────────────────────────────────────────┐
│  3. Image Validation                    │
│     - File exists on disk?              │
│     - Supported format (PNG/JPG/WebP/GIF)?│
│     - Not a remote URL (http://)?       │
└─────────────────────────────────────────┘
           ⬇
┌─────────────────────────────────────────┐
│  4. Model Selection                     │
│     - Has valid images → voyage-multimodal-3│
│     - Code/text only → voyage-code-3    │
└─────────────────────────────────────────┘
           ⬇
┌─────────────────────────────────────────┐
│  5. Embedding & Storage                 │
│     - Generate embedding with selected model│
│     - Store in corresponding collection │
└─────────────────────────────────────────┘
```

**Supported Image Formats**: PNG, JPG/JPEG, WebP, GIF
**Skipped**: Remote URLs (http://, https://), missing files, unsupported formats (.bmp, .svg)

### Parallel Multi-Index Query

When both collections exist, queries search them **concurrently** using ThreadPoolExecutor:

```
Multi-Index Query Pipeline:
┌─────────────────────────────────────────────────────────────┐
│  Check: Does voyage-multimodal-3 collection exist?          │
│  - Yes → Parallel dual-index query                          │
│  - No  → Single-index query (voyage-code-3 only)            │
└─────────────────────────────────────────────────────────────┘
           ⬇ (if multimodal exists)
┌──────────────────────────┐     ┌──────────────────────────┐
│  Thread 1: Code Index    │     │  Thread 2: Multimodal    │
│  - voyage-code-3 query   │     │  - voyage-multimodal-3   │
│  - HNSW search           │     │  - HNSW search           │
│  - Return top N*2        │     │  - Return top N*2        │
└──────────────────────────┘     └──────────────────────────┘
           ⬇ Parallel Execution (wall-clock = max of both) ⬇
┌─────────────────────────────────────────────────────────────┐
│  Result Merging                                             │
│  1. Combine results from both indexes                       │
│  2. Deduplicate by (file_path, chunk_offset)                │
│     - Keep highest score when duplicates exist              │
│  3. Sort by score descending                                │
│  4. Apply limit to final results                            │
└─────────────────────────────────────────────────────────────┘
```

**Timing Semantics**:
- `parallel_multi_index_ms`: Wall-clock time for both queries (= max of individual times)
- `code_index_ms`: Wall-clock time for voyage-code-3 query
- `multimodal_index_ms`: Wall-clock time for voyage-multimodal-3 query
- `merge_deduplicate_ms`: Time to merge and deduplicate results

**Invariant**: `parallel_multi_index_ms >= max(code_index_ms, multimodal_index_ms)`

**Timeout Handling**: Each index has independent 30-second timeout. If one times out, partial results from the successful index are still returned.

### Backward Compatibility

- **No multimodal content**: System operates exactly as before (single voyage-code-3 collection)
- **Existing indexes**: Multimodal collection only created when markdown files with valid images are indexed
- **Query interface**: Same `cidx query` command works transparently
- **CLI feedback**: Shows `Using: voyage-code-3, voyage-multimodal-3` when both active

### Search Strategy Evolution

**Version 6.x: Binary Index (O(N) Linear Scan)**
```python
# Load ALL vectors
for vector_id in all_vectors:  # O(N)
    vector = load_vector(vector_id)
    similarity = cosine(query, vector)
    results.append((vector_id, similarity))

results.sort()  # O(N log N)
return results[:limit]

# Performance: 6+ seconds for 7K vectors
```

**Version 7.0: HNSW Index (O(log N) Graph Search)**
```python
# Load HNSW graph index
hnsw = load_hnsw_index()  # O(1)

# Navigate graph to find approximate nearest neighbors
candidates = hnsw.search(query, k=limit*2)  # O(log N)

# Load ONLY candidate vectors
for candidate_id in candidates:  # O(K) where K << N
    vector = load_vector(candidate_id)
    similarity = exact_cosine(query, vector)
    if filter_match(vector.payload):
        results.append((candidate_id, similarity))

results.sort()  # O(K log K)
return results[:limit]

# Performance: ~20ms for 37K vectors (300x faster)
```

## Incremental HNSW Updates (v7.2+)

Code Indexer v7.2 introduced **incremental HNSW index updates**, eliminating expensive full rebuilds.

**Performance:**
- **Watch mode updates**: < 20ms per file (vs 5-10s full rebuild) - **99.6% improvement**
- **Batch indexing**: 1.46x-1.65x speedup for incremental updates
- **Zero query delay**: First query after changes returns instantly
- **Overall**: **3.6x average speedup** in typical development workflows

**How It Works:**
- **Change Tracking**: Tracks added/updated/deleted vectors during indexing session
- **Auto-Detection**: SmartIndexer automatically determines incremental vs full rebuild
- **Label Management**: Efficient ID-to-label mapping maintains consistency
- **Soft Delete**: Deleted vectors marked (not removed) to avoid rebuilds

**When Incremental Updates Apply:**
- ✅ **Watch mode**: File changes trigger real-time HNSW updates
- ✅ **Re-indexing**: Subsequent `cidx index` runs use incremental updates
- ✅ **Git workflow**: Changes after `git pull` indexed incrementally
- ❌ **First-time indexing**: Full rebuild required (no existing index)
- ❌ **Force reindex**: `cidx index --clear` explicitly forces full rebuild

## Performance Decision Analysis

**Why HNSW?**
1. **vs FAISS**: Simpler integration, no external C++ dependencies, optimal for small-medium datasets (<100K vectors)
2. **vs Annoy**: Better accuracy-speed tradeoff, superior graph connectivity
3. **vs Product Quantization**: Maintains full precision, no accuracy loss
4. **vs Brute Force**: 300x speedup justifies ~150MB index overhead

**Quantization Strategy:**
- **64-dim projection**: Optimal balance (tested 32, 64, 128, 256 dimensions)
- **4-level depth**: Enables 64^4 = 16.8M unique paths (sufficient for large codebases)
- **2-bit quantization**: Further reduces from 64 to 4 levels per dimension

**Storage Trade-offs:**
- **JSON vs Binary**: JSON chosen for git-trackability and debuggability (3-5x size acceptable)
- **Individual files**: Enable incremental updates and git change tracking
- **Binary exceptions**: ID index and HNSW use binary for performance-critical components

## Parallel Processing Architecture

Code Indexer uses slot-based parallel file processing for efficient throughput:

**Architecture:**
- **Dual thread pool design** - Frontend file processing (threadcount+2 workers) feeds backend vectorization (threadcount workers)
- **File-level parallelism** - Multiple files processed concurrently with dedicated slot allocation
- **Slot-based allocation** - Fixed-size display array (threadcount+2 slots) with natural worker slot reuse
- **Real-time progress** - Individual file status visible during processing (starting → chunking → vectorizing → finalizing → complete)

**Thread Configuration:**
- **VoyageAI default**: 8 vectorization threads → 10 file processing workers (8+2)
- **Ollama default**: 1 vectorization thread → 3 file processing workers (1+2)
- **Frontend thread pool**: threadcount+2 workers handle file reading, chunking, and coordination
- **Backend thread pool**: threadcount workers handle vector embedding calculations
- **Pipeline design**: Frontend stays ahead of backend, ensuring continuous vector thread utilization

## Model-Aware Chunking Strategy

Code Indexer uses a **model-aware fixed-size chunking approach** optimized for different embedding models:

**How it works:**
- **Model-optimized chunk sizes**: Automatically selects optimal chunk size based on embedding model capabilities
- **Consistent overlap**: 15% overlap between adjacent chunks (across all models)
- **Simple arithmetic**: Next chunk starts at (chunk_size - overlap_size) from current start position
- **Token optimization**: Uses larger chunk sizes for models with higher token capacity

**Model-Specific Chunk Sizes:**
- **voyage-code-3**: 4,096 characters (leverages 32K token capacity)
- **voyage-code-2**: 4,096 characters (leverages 16K token capacity)
- **voyage-large-2**: 4,096 characters (leverages large context capacity)
- **nomic-embed-text**: 2,048 characters (512 tokens - Ollama limitation)
- **Unknown models**: 1,000 characters (conservative fallback)

**Example chunking (voyage-code-3):**
```
Chunk 1: characters 0-4095     (4096 chars)
Chunk 2: characters 3482-7577  (4096 chars, overlaps 614 chars with Chunk 1)
Chunk 3: characters 6964-11059 (4096 chars, overlaps 614 chars with Chunk 2)
```

**Benefits:**
- **Model optimization**: Uses larger chunks for high-capacity models
- **Better context**: More complete code sections per chunk
- **Efficiency**: Fewer total chunks reduce storage requirements
- **Model utilization**: Takes advantage of each model's token capacity

## Full-Text Search (FTS) Architecture

CIDX integrates Tantivy-based full-text search alongside semantic search.

**Performance:**
- **1.36x faster than grep** on indexed codebases
- **Parallel execution** in hybrid mode (both searches run simultaneously)
- **Real-time index updates** in watch mode
- **Storage**: `.code-indexer/tantivy_index/`

**FTS Incremental Indexing (v7.2+):**
- **FileFinder integration**: 30-36x faster rebuild (6.3s vs 3+ minutes)
- **Incremental updates**: Tantivy updates only changed documents
- **Automatic detection**: Checks for `meta.json` to detect existing index

## Git History Search (Temporal Indexing)

CIDX can index and semantically search entire git commit history:

**What Gets Indexed:**
- Commit messages (full text, not truncated)
- Code diffs for each commit
- Commit metadata (author, date, hash)
- Branch information

**Query Capabilities:**
- Search entire git history semantically
- Filter by time ranges (specific dates or `--time-range-all`)
- Filter by chunk type (`commit_message` or `commit_diff`)
- Filter by author
- Combine with language and path filters

**Use Cases:**
- Code archaeology - when was code introduced
- Bug history research
- Feature evolution tracking
- Author code analysis

## MCP Protocol Integration

**Protocol Version**: `2025-06-18` (Model Context Protocol)

**Initialize Handshake** (CRITICAL for Claude Code connection):
- Method: `initialize` - MUST be first client-server interaction
- Server Response: `{ "protocolVersion": "2025-06-18", "capabilities": { "tools": {} }, "serverInfo": { "name": "Neo", "version": "9.3.3" } }`
- Required for OAuth flow completion - Claude Code calls `initialize` after authentication

**Version Notes**:
- Updated from `2024-11-05` to `2025-06-18` for Claude Desktop compatibility
- 2025-06-18 breaking changes: Removed JSON-RPC batching support
- 2025-06-18 new features: Structured tool output, OAuth resource parameter (RFC 8707), elicitation/create for server-initiated user input
- Current implementation: Version updated, feature audit pending

**Tool Response Format** (CRITICAL for Claude Code compatibility):
- All tool results MUST return `content` as an **array of content blocks**, NOT a string
- Each content block must have: `{ "type": "text", "text": "actual content here" }`
- Empty content should be `[]`, NOT `""` or missing
- Error responses must also include `content: []` (empty array is valid)

## Vector Storage Backends

### Filesystem Backend (Default)

Container-free vector storage using the local filesystem:

**Features:**
- **No containers required** - Stores vector data directly in `.code-indexer/index/`
- **Zero setup overhead** - Works immediately without Docker/Podman
- **Lightweight** - Minimal resource footprint
- **Portable** - Vector data travels with your repository

**When to use**: Development environments, CI/CD pipelines, container-restricted systems

### Qdrant Backend (Removed in v8.0)

**Historical Note**: Qdrant container-based backend was removed in v8.0 as part of the architectural simplification. CIDX now uses only FilesystemVectorStore with HNSW indexing, providing comparable performance without container dependencies.

For migration from v7.x Qdrant deployments, see [Migration Guide](migration-to-v8.md).

## Self-Monitoring Architecture (v8.8.2+)

CIDX Server includes automated self-monitoring using Claude CLI for intelligent log analysis.

**Components:**
- **SelfMonitoringService**: Background scheduler running at configurable intervals (default 60 min)
- **LogScanner**: Executes Claude CLI with structured prompts for log analysis
- **IssueManager**: Creates GitHub issues for detected bugs

**Workflow:**
```
Scheduled Scan:
1. Service submits job to BackgroundJobManager
2. LogScanner assembles prompt with log database path
3. Claude CLI queries SQLite logs directly (--allowedTools Bash)
4. Claude analyzes logs and returns structured JSON (bugs found/not found)
5. IssueManager creates GitHub issues for actionable bugs
6. Scan results stored in self-monitoring database
```

**Key Design Decisions:**
- **Claude queries DB directly**: Prompt contains database path, not embedded logs (5KB vs 548KB)
- **Auto-detect github_repo**: Extracted from git remote origin (no env vars required)
- **Actionable focus**: Prompt filters configuration noise, reports only development bugs
- **Working directory context**: Claude runs with `cwd=repo_root` for full codebase access

**Storage:**
- Scan history: `~/.cidx-server/data/self_monitoring.db`
- Server logs: `~/.cidx-server/logs.db` (SQLite structured logging)

## Research Session Tracing (Langfuse) (v8.10.0+)

Optional observability integration for tracking MCP tool usage patterns via Langfuse.

**Purpose:**
- Track research sessions with explicit start/end boundaries
- Capture all MCP tool calls as spans with timing, inputs, outputs
- Enable performance analysis and usage pattern discovery
- Support session scoring and feedback for quality assessment

**MCP Tools:**
- `start_trace(name, metadata)` - Begin a research session trace
- `end_trace(score, feedback)` - Complete trace with optional quality score (0.0-1.0)

**Architecture Components:**
- **LangfuseService** (`langfuse_service.py`) - Facade providing lazy-initialized access to Langfuse components
- **LangfuseClient** (`langfuse_client.py`) - SDK wrapper using Langfuse 3.7.0 API
- **TraceStateManager** (`trace_state_manager.py`) - Per-session trace context management
- **AutoSpanLogger** (`auto_span_logger.py`) - Automatic span creation for MCP tool calls

**Key Design Decisions:**
- **Graceful degradation**: Langfuse errors never fail upstream MCP operations
- **Opt-in tracing**: Disabled by default, configurable via Web UI
- **Auto-trace mode**: Optional automatic trace creation on first tool call
- **Session persistence**: HTTP clients use `?session_id=xxx` for trace continuity across requests
- **RLock for thread safety**: Prevents deadlock during lazy initialization of nested components

**Configuration** (Web UI: Admin > Configuration > Langfuse Settings):
- `enabled` - Enable/disable Langfuse integration
- `public_key` / `secret_key` - Langfuse API credentials
- `host` - Langfuse server URL (default: cloud.langfuse.com)
- `auto_trace_enabled` - Automatically create trace on first tool call

**Storage:**
- Configuration: `~/.cidx-server/config.json` (langfuse section)
- Traces: Stored in Langfuse backend (cloud or self-hosted)

## Inter-Repository Dependency Map (v9.0+)

Multi-pass Claude CLI pipeline that analyzes source code across all registered golden repos to produce domain-level dependency documentation.

**Pipeline Passes:**
1. **Pass 1 (Synthesis)**: Single Claude CLI call clusters repos into semantic domains based on naming, README content, and shared patterns. Outputs JSON with domain names, descriptions, participating repos, and evidence.
2. **Pass 2 (Per-Domain Analysis)**: One Claude CLI subprocess per domain. Each session has MCP tool access to CIDX for searching repos. Produces per-domain `.md` files with intra-domain dependencies, cross-domain connections, and file-level citations.
3. **Pass 3 (Index Generation)**: Deterministic Python post-processing. Builds domain catalog, repo-to-domain matrix, and cross-domain dependency graph. No LLM involvement.

**Cross-Domain Dependency Graph (v9.2+):**

Pass 3 parses all domain `.md` files to construct a directed edge list showing which domains reference other domains' repositories. The algorithm:
1. Builds a reverse mapping from repo aliases to their owning domains
2. Extracts the "Cross-Domain Connections" section from each domain file
3. Splits into paragraphs and filters out negation paragraphs (containing phrases like "zero results", "unrelated", "not functional")
4. Searches non-negated text for other domains' repo aliases using word-boundary regex
5. Produces a markdown table appended to `_index.md` with source domain, target domain, and connecting repos

**Key Design Decisions:**
- **Journal-based resumability**: `_journal.json` tracks pass completion for crash recovery
- **Stage-then-swap atomicity**: Pipeline writes to staging directory, renamed to final on completion
- **Inside-out analysis**: Pass 2 starts from the largest repo in each domain and works outward
- **Conciseness enforcement**: PostToolUse hooks remind Claude CLI to stay within output size limits
- **Paragraph-level negation filtering**: Prevents false positives from isolation confirmation text

**Storage:**
- Output: `~/.cidx-server/data/golden-repos/cidx-meta/dependency-map/`
- Journal: `dependency-map/_journal.json`
- Index: `dependency-map/_index.md`

**Configuration** (Web UI: Admin > Configuration > Dependency Map):
- `dependency_map_enabled`: Feature toggle (default: off)
- `dependency_map_interval_hours`: Delta refresh interval (default: 168 hours / weekly)
- `dependency_map_pass_timeout_seconds`: Per-pass timeout (default: 1800s)
- `dependency_map_pass{1,2,3}_max_turns`: Claude CLI turn limits per pass

## Related Documentation

- **[Algorithms](algorithms.md)** - Detailed algorithm descriptions and complexity analysis
- **[Technical Details](technical-details.md)** - Deep dives into implementation specifics
