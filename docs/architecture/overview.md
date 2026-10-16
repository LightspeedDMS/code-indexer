# Architecture Overview

A map of CIDX for maintainers and contributors: the components, the ways they are deployed, how data flows through
indexing and querying, and where each subsystem is described in detail. Rules the code must keep are in
[Invariants](invariants/README.md).

## Components

| Component | Code | Role |
|-----------|------|------|
| CLI | `src/code_indexer/cli.py` (`cidx`) | init, index, query, watch; also the client for remote mode |
| Indexer | `src/code_indexer/services/` (`SmartIndexer`, `HighThroughputProcessor`, `FileChunkingManager`) | git-aware discovery, chunking, embedding, storage |
| Vector store | `src/code_indexer/storage/filesystem_vector_store.py`, `hnsw_index_manager.py` | per-collection HNSW graph plus chunk records (JSON shards or `chunks.db`) |
| Full-text index | `src/code_indexer/services/tantivy_index_manager.py` | Tantivy index for FTS and regex queries |
| Temporal indexer | `src/code_indexer/services/temporal/` | one document per commit, quarterly shards per embedder |
| Embedding providers | `src/code_indexer/services/voyage_ai.py`, `cohere_embedding.py` and multimodal variants | VoyageAI and Cohere, plus rerankers |
| SCIP | `src/code_indexer/scip/` | precise definitions, references and call chains from `index.scip.db` |
| X-Ray | `src/code_indexer/xray/`, `rust/xray-core`, `rust/xray-cli` | AST-aware search with user-supplied Rust evaluators |
| Daemon | `src/code_indexer/daemon/` | per-project RPyC service that keeps indexes in memory |
| Server | `src/code_indexer/server/` | FastAPI app: REST API, MCP endpoint (`/mcp`), Web UI, background jobs, schedulers |
| Auto-updater | `src/code_indexer/server/auto_update/` | per-node systemd timer that deploys new code and applies launch settings |

## Operating modes

| Mode | How it runs | Storage |
|------|-------------|---------|
| CLI | `cidx index` / `cidx query` in a repository; each query loads indexes from disk | `.code-indexer/` in the repository |
| Daemon | `cidx config --daemon`; the CLI forwards commands to a per-project RPyC daemon over a Unix socket under `/tmp/cidx/` (hash-named) that caches HNSW and FTS indexes and runs `cidx watch` | same as CLI |
| Remote client | `cidx init --remote <url>`; commands marked remote in `cidx --help` call a CIDX server | the server's |
| Proxy | `cidx init --proxy-mode`; one directory that fans commands out to several indexed repositories | each repository's own |
| Server, solo | one node, `storage_mode: sqlite` | SQLite databases under `~/.cidx-server/`, repositories under `~/.cidx-server/data/` |
| Server, cluster | several nodes, `storage_mode: postgres` | shared PostgreSQL plus one shared CoW storage mount per node |

User-facing detail of the local modes: [Operating Modes](../getting-started/operating-modes.md). Cluster design:
[Cluster](cluster.md).

## Indexing data flow

1. Discover files: full walk on the first run, git topology (`git diff` between commits, branch awareness) afterwards;
   the same exclusion rules apply to both.
2. Chunk each file with model-aware sizes and overlap; markdown or HTML with valid local images goes to the
   multimodal collection.
3. Embed chunks in token-aware batches through the provider; only per-request HTTP timeouts apply.
4. Store: upsert points into the collection (one per embedding model), update the HNSW graph and the FTS index.
   Clean git files store a blob reference, dirty and non-git files store the text.
5. Temporal indexing (`cidx index --index-commits`) builds one document per commit into quarterly shards.

Detail: [Indexing](indexing.md), [Storage](storage.md).

## Query data flow

1. Embed the query (server: query-embedding cache, then coalescer and per-lane concurrency governor; CLI: direct
   provider call).
2. Search the HNSW graph of each collection (code and multimodal in parallel), hydrate the top candidates from the
   chunk store, apply filters, merge.
3. FTS and regex queries use the Tantivy index; hybrid mode runs semantic and FTS together. Temporal queries search
   the commit shards for a time range.
4. Server only: optional reranking, memory retrieval for semantic and hybrid `search_code`, and payload truncation with
   cached full payloads (`PayloadCache`).

Detail: [Query Path](query-path.md).

## Server

- Startup (`src/code_indexer/server/startup/`): `service_init.py` builds the backend registry and services,
  `lifespan.py` wires caches, schedulers and cluster services, `app_wiring.py` publishes them on `app.state`. Importing
  `code_indexer.server.app` has no side effects; the app is built on first access.
- Configuration: bootstrap keys in `~/.cidx-server/config.json` (`BOOTSTRAP_KEYS` in
  `src/code_indexer/server/services/config_service.py`); every other setting is a runtime setting in the database,
  changed through the Web UI. Operator view: [Deployment](../server/deployment.md).
- Repositories: golden repositories are cloned and indexed on the server, published as immutable snapshots behind
  `-global` aliases, and copied per user on activation. Detail: [Repository Lifecycle](repository-lifecycle.md).
- Background work runs as jobs through `BackgroundJobManager` / `JobTracker`: golden-repository refresh, description
  refresh, dependency-map analysis, HNSW orphan sweep, fleet migration, retention sweeps. Failed refreshes recover as
  described in [Refresh Recovery](refresh-recovery.md).
- Authentication: JWT sessions, API keys, MCP credentials, OIDC, TOTP step-up elevation for admin operations.
  Operator view: [Login and Elevation](../server/auth/login-and-elevation.md).
- Long-running process hygiene (connection cleanup daemon, cache caps, malloc mitigations):
  [Server Runtime Invariants](invariants/server-runtime.md).

## Where to read next

| Subject | Document |
|---------|----------|
| Rules the code must keep | [Invariants](invariants/README.md) |
| Indexing algorithm | [Indexing](indexing.md) |
| Index storage, chunk layouts, migration | [Storage](storage.md) |
| Query path and query-embedding cache | [Query Path](query-path.md) |
| Golden, global and activated repositories | [Repository Lifecycle](repository-lifecycle.md) |
| Refresh failure handling | [Refresh Recovery](refresh-recovery.md) |
| Cluster mode | [Cluster](cluster.md) |
| Dependency map and cidx-meta backup | [Dependency Map](dependency-map.md) |
| X-Ray | [X-Ray architecture](xray/architecture.md), [Sandbox](xray/sandbox.md), [Graph binder](xray/graph-binder-internals.md) |
| CLI reference | [CLI Reference](../reference/cli/README.md) |
| Server operation | [Deployment](../server/deployment.md) |
