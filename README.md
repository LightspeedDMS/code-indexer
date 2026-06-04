# Code Indexer (`cidx`)

AI-powered semantic code search for your codebase. Find code by meaning, not just keywords.

[![CI/CD](https://img.shields.io/github/actions/workflow/status/LightspeedDMS/code-indexer/main.yml?branch=master&label=CI%2FCD)](https://github.com/LightspeedDMS/code-indexer/actions/workflows/main.yml) [![Release](https://img.shields.io/github/v/release/LightspeedDMS/code-indexer?sort=semver&color=blue)](https://github.com/LightspeedDMS/code-indexer/releases) [![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue)](https://www.python.org/) [![License: MIT](https://img.shields.io/github/license/LightspeedDMS/code-indexer?color=green)](LICENSE)

[Changelog](CHANGELOG.md) | [Migration Guide](docs/migration-to-v10.md) | [Architecture](docs/architecture.md)

## What is CIDX?

CIDX is an end-to-end code intelligence system for finding, navigating, and reasoning about source code by meaning rather than by tokens. It combines semantic search (VoyageAI or Cohere embeddings on an HNSW vector index, O(log N) lookups) with cross-encoder reranking (Voyage rerank-2.5 or Cohere rerank, applied after RRF coalescing) for quality-multiplied results, full-text and regex retrieval via Tantivy, SCIP-backed symbol navigation, AST-level structural search through tree-sitter, and git-history temporal search -- all running container-free out of `.code-indexer/`.

- **Find code by meaning, by name, or by structure** -- natural-language queries ("authentication logic", "where the rate limiter rejects"), exact / regex / fuzzy FTS, SCIP definitions / references / call chains / dependency graphs / impact analysis, and X-Ray AST evaluators for structural patterns beyond text. Multimodal indexing pulls in diagrams and screenshots embedded in markdown and HTML automatically. A configurable cross-encoder reranking stage (Voyage rerank-2.5 or Cohere rerank) lifts the top-N from "semantically related" to "actually answers the query".
- **Token-efficient for AI agents** -- X-Ray AST search lets an agent ask "find every method longer than 50 lines that catches and rethrows without logging" and get back the exact matching ranges instead of pulling whole files into context to scan. A user-defined Rust evaluator runs server-side in a sandbox over a tree-sitter AST and returns structured findings -- orders of magnitude cheaper in tokens than loading and re-parsing files in the agent's window. Combined with SCIP for precise symbol navigation and the multi-modal MCP surface, agents do less reading and more reasoning.
- **Reason across time and across repos** -- commit-history semantic search with time-range and author filters, Langfuse trace sync that makes AI conversation history searchable alongside your code, and a Claude-driven inter-repository dependency map that builds a queryable cross-repo domain graph for change-impact reasoning.
- **Scale from laptop to cluster** -- start as a CLI, upgrade to a watching daemon with in-process index caching, or deploy a multi-user Server with OAuth 2.0 / OIDC + TOTP MFA + step-up elevation, role-based permissions, REST + MCP APIs (`/mcp` with JWT, `/mcp-public` unauthenticated), semantic memory retrieval, golden-repository management, HNSW caching, and a web dashboard. Cluster mode shares state across nodes via PostgreSQL with leader election and distributed job queuing. Embeddings are multi-provider: VoyageAI or Cohere with primary-only, failover, parallel RRF fusion, or explicit-provider strategies.

<details>
<summary>Table of Contents</summary>

- [What is CIDX?](#what-is-cidx)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Key Features](#key-features)
- [Operating Modes](#operating-modes)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Security](#security)
- [Contributing](#contributing)
- [License](#license)

</details>

## Installation

```bash
pipx install git+https://github.com/LightspeedDMS/code-indexer.git@master
cidx --version
```

**Requirements**: Python 3.9-3.12, 4GB+ RAM, VoyageAI API key (or Cohere API key).
For platform-specific instructions, Windows setup, and troubleshooting, see [Installation Guide](docs/installation.md).

## Quick Start

```bash
cd /path/to/your/project

# Set embedding provider API key (VoyageAI default; Cohere also supported)
export VOYAGE_API_KEY="your-api-key"

# Index and search
cidx index
cidx query "authentication logic" --limit 5
cidx query "user" --language python --min-score 0.7
cidx query "save" --path-filter "*/models/*" --limit 10
```

For comprehensive query options and search strategies, see [Query Guide](docs/query-guide.md).

## Key Features

### Semantic Search

Find code by meaning using AI embeddings powered by VoyageAI or Cohere. Natural language queries return semantically relevant results ranked by similarity.

```bash
cidx query "authentication logic" --limit 10
cidx query "database connection setup" --language python
```

### Multimodal Search

Search documentation that includes diagrams, screenshots, and visual content. CIDX automatically detects and indexes images embedded in markdown and HTML files using multimodal embeddings -- no special flags needed.

See: [Architecture Guide](docs/architecture.md#dual-model-architecture-v88)

### Full-Text Search (FTS)

Fast exact text matching with fuzzy search, regex support, and case sensitivity options. Up to 50x faster than grep with indexed searching. Combine `--fts` with `--semantic` for hybrid search that fuses keyword and meaning-based ranking.

```bash
cidx query "authenticate_user" --fts
cidx query "test_.*" --fts --regex --language python
cidx query "auth" --fts --semantic         # hybrid: keyword + semantic
```

See: [Hybrid Search](docs/query-guide.md#hybrid-search)

### SCIP Code Intelligence

Precise code navigation using SCIP (Source Code Intelligence Protocol). Find definitions, references, dependencies, call chains, and perform impact analysis.

```bash
cidx scip generate                    # Generate SCIP indexes
cidx scip definition "UserService"    # Find definition
cidx scip references "authenticate"   # Find all usages
cidx scip callchain "main" "login"    # Trace execution path
```

See: [SCIP Code Intelligence Guide](docs/scip/README.md)

### Git History Search

Search your entire commit history semantically with time-range and author filtering.

```bash
cidx index --index-commits
cidx query "JWT auth" --time-range-all
cidx query "bug fix" --time-range 2024-01-01..2024-12-31
```

See: [Temporal Search Guide](docs/temporal-search.md)

### Real-Time Watch Mode

Background daemon with in-memory HNSW/FTS index caching (eliminating the per-invocation cold load) and automatic re-indexing on file changes. End-to-end query latency remains bounded by the embedding-provider round trip.

```bash
cidx config --daemon && cidx start
cidx watch
```

See: [Operating Modes Guide](docs/operating-modes.md#daemon-mode)

### AI Integration

Connect AI assistants to CIDX for semantic search in conversations. Supports local CLI integration (Claude Code, Gemini, Codex, OpenCode, Q, Junie) and remote MCP server endpoints (`/mcp` with JWT, `/mcp-public` unauthenticated).

```bash
cidx teach-ai --claude --project    # Local CLI integration
```

See: [AI Integration Guide](docs/ai-integration.md)

### Langfuse Trace Sync

Pull AI conversation traces from Langfuse and make them semantically searchable alongside your code. Background sync, smart deduplication, and automatic indexing.

See: [Langfuse Trace Sync Guide](docs/langfuse-trace-sync.md)

### Inter-Repository Dependency Map

A Claude-driven analysis pipeline maps domain-level relationships across all registered golden repos and stores them as a queryable, directed dependency graph. Through the server's MCP tools, AI agents can retrieve the full cross-domain graph, identify hub domains, find which domains consume a given domain, and detect stale domains that need re-analysis -- enabling cross-repository discovery and change-impact reasoning.

See: [Meta-Repo Discovery Guide](docs/meta-repo-discovery.md)

### Multi-Provider Embedding

Supports VoyageAI (default) and Cohere providers with configurable query strategies: primary-only, failover, parallel fusion (RRF), or explicit provider targeting.

See: [Configuration Guide](docs/configuration.md#embedding-provider)

### X-Ray AST Search

Tree-sitter-powered AST analysis with sandboxed Python evaluators. Write custom evaluators that operate on parsed syntax trees for structural code search beyond text matching.

See: [X-Ray Architecture](docs/xray-architecture.md) | [X-Ray Cookbook](docs/xray-cookbook.md)

## Operating Modes

| Mode | Cache surface | Best For | Details |
|------|---------------|----------|---------|
| **CLI** | None (per-invocation cold load) | Individual developers, quick searches | [Operating Modes](docs/operating-modes.md#cli-mode) |
| **Daemon** | In-process HNSW/FTS cache, single user | Active development, watch mode | [Operating Modes](docs/operating-modes.md#daemon-mode) |
| **Server** | In-process HNSW/FTS cache, multi-user | Team collaboration, multi-user | [Server Deployment](docs/server-deployment.md) |
| **Cluster** | Per-node HNSW/FTS cache, shared PostgreSQL state | High availability, horizontal scaling | [Cluster Setup](docs/cluster-setup.md) |

End-to-end query latency is dominated by the embedding-provider round trip (50–300ms typical for VoyageAI / Cohere); the cache surface column above describes only how each mode amortizes the in-process index lookup. See [Operating Modes Guide](docs/operating-modes.md) for measured HNSW lookup numbers and the methodology behind them.

**Server Mode** provides multi-user access with OAuth 2.0/OIDC authentication, TOTP MFA, role-based permissions, REST API, MCP protocol, golden repository management, cross-encoder reranking, semantic memory retrieval, inter-repository dependency mapping, HNSW caching, and web administration. See [Operating Modes Guide](docs/operating-modes.md#server-mode) for the full feature set.

**Cluster Mode** extends Server Mode across multiple nodes sharing PostgreSQL with leader election, distributed job queuing, and cross-node configuration propagation. See [Cluster Architecture](docs/cluster-architecture.md).

## Configuration

CIDX requires a VoyageAI or Cohere API key. Project settings auto-generate in `.code-indexer/config.json` on first run.

See: [Configuration Guide](docs/configuration.md)

## Documentation

### Getting Started

- [Installation Guide](docs/installation.md) -- Setup for all platforms
- [Query Guide](docs/query-guide.md) -- All query parameters and search strategies
- [Configuration Guide](docs/configuration.md) -- API keys, config options, environment variables

### Features

- [SCIP Code Intelligence](docs/scip/README.md) -- Symbol navigation, dependencies, call chains
- [Temporal Search](docs/temporal-search.md) -- Git history search with time-range filtering
- [Operating Modes](docs/operating-modes.md) -- CLI, Daemon, Server modes explained
- [X-Ray Architecture](docs/xray-architecture.md) -- AST search engine and sandbox
- [X-Ray Cookbook](docs/xray-cookbook.md) -- Evaluator examples and patterns

### AI Integration

- [AI Integration Guide](docs/ai-integration.md) -- Connect AI assistants to CIDX
- [Langfuse Trace Sync](docs/langfuse-trace-sync.md) -- Searchable AI conversation history
- [Meta-Repo Discovery](docs/meta-repo-discovery.md) -- Cross-repo dependency mapping
- [Guardrails Convention](docs/guardrails-repo-convention.md) -- Safety guardrails for delegation jobs
- [Delegation Functions](docs/delegation-functions.md) -- AI workflows for code review and analysis

### Server Administration

- [Server Deployment](docs/server-deployment.md) -- Deploy and operate CIDX Server
- [Cluster Architecture](docs/cluster-architecture.md) -- Multi-node design and storage abstraction
- [Cluster Setup](docs/cluster-setup.md) -- Install and operate a cluster with PostgreSQL
- [CoW Storage Setup](docs/cow-storage-setup.md) -- Configure CoW Storage Daemon as shared cluster storage
- [OIDC Setup](docs/oidc-setup-and-configuration.md) -- OpenID Connect SSO configuration
- [TOTP Elevation](docs/totp-elevation.md) -- Step-up authentication for admin operations
- [Auto-Update Guide](docs/auto-update.md) -- Job-aware updates with graceful drain mode
- [Fault Injection](docs/fault-injection-operator-guide.md) -- Resilience testing harness (non-prod)
- [Server Memory Invariants](docs/server-memory-invariants.md) -- Cache tuning and memory management

### Architecture

- [Architecture Guide](docs/architecture.md) -- System design and storage architecture
- [Dep-Map Parser](docs/depmap-parser-architecture.md) -- Dependency map module design
- [Memory Retrieval](docs/memory-retrieval-operator-guide.md) -- Semantic memory pipeline
- [Migration to v10](docs/migration-to-v10.md) -- Upgrading from v9.x
- [Migration to v8](docs/migration-to-v8.md) -- Upgrading from v7.x
- [Changelog](CHANGELOG.md) -- Version history and release notes

## Security

Found a vulnerability? Please report it privately -- see [SECURITY.md](SECURITY.md). Do not open a public issue for security reports. The authentication stack, X-Ray evaluator sandbox, and multi-user deployment surfaces are documented under [docs/security/](docs/security/).

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing guidelines, and code quality standards. Please also review our [Code of Conduct](CODE_OF_CONDUCT.md).

- **Bugs**: [GitHub Issues](https://github.com/LightspeedDMS/code-indexer/issues)
- **Features**: [GitHub Issues](https://github.com/LightspeedDMS/code-indexer/issues)
- **Questions**: [GitHub Discussions](https://github.com/LightspeedDMS/code-indexer/discussions)

## License

Released under the [MIT License](LICENSE).

---

**Repository**: [https://github.com/LightspeedDMS/code-indexer](https://github.com/LightspeedDMS/code-indexer)
