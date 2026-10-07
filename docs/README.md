# CIDX documentation

The documents under `docs/`, grouped by who they are for. The generated reference sections (CLI, MCP tools, error
codes) contain one page per command group, tool category or code area; each is indexed by its own README page,
linked under Reference. Start with [Installation](getting-started/installation.md) if you are new; the project
overview is in the [repository README](../README.md).

## Getting started

- [Installation](getting-started/installation.md): install, verify, upgrade and remove the `cidx` CLI.
- [Configuration](getting-started/configuration.md): embedding provider keys, `.code-indexer/config.json` and the override file.
- [Operating modes](getting-started/operating-modes.md): CLI, daemon, remote client, server and cluster, and how to switch.
- [Teaching an AI assistant (`cidx teach-ai`)](getting-started/teach-ai.md): install the cidx awareness section and skills for local AI assistants.
- [MCP registration](getting-started/mcp-registration.md): connect Claude Code or another MCP client to a CIDX server.

## Guides

- [Query guide](guides/query.md): semantic, full-text, regex and hybrid search, filters and options.
- [Temporal search](guides/temporal-search.md): indexing and searching git history.
- [SCIP code intelligence](guides/scip.md): definitions, references, dependencies, call chains and impact analysis.
- [X-Ray cookbook](guides/xray-cookbook.md): example X-Ray AST evaluators and patterns (compiled into the X-Ray binary).
- [Meta-repo discovery](guides/meta-repo-discovery.md): cross-repository discovery and the dependency map through the server.

## Server operators

- [Deployment](server/deployment.md): install, configure and run the CIDX server.
- [Upgrading](server/upgrading.md): the entry point for moving an installed server to a newer version.
- [Admin guide](server/admin-guide.md): the Web UI administration areas.
- [Maintenance and jobs](server/maintenance-and-jobs.md): maintenance mode and draining, the jobs dashboard, job cancellation.
- [Observability](server/observability.md): health endpoints, logs, admin log queries and OpenTelemetry export.
- [Login and elevation](server/auth/login-and-elevation.md): password, MFA and SSO sign-in, TOTP step-up elevation, login throttling.
- [OIDC](server/auth/oidc.md): single sign-on with an OpenID Connect provider.
- [Auto-update](server/auto-update.md): the job-aware auto-updater and its self-healing deployment steps.
- [Cluster setup](server/cluster-setup.md): install and operate a multi-node cluster on PostgreSQL.
- [CoW storage setup](server/cow-storage-setup.md): the copy-on-write storage daemon as shared cluster storage.
- [hnswlib custom build](server/hnswlib-custom-build.md): the hnswlib fork CIDX requires and how to build it.
- [Data migration playbook](server/data-migration-playbook.md): moving server data between storage backends.
- [Fault injection](server/fault-injection.md): the non-production fault-injection harness.
- [Memory retrieval](server/memory-retrieval.md): the semantic memory retrieval pipeline and its settings.
- [Langfuse trace sync](server/langfuse-trace-sync.md): pulling Langfuse traces into searchable repositories.
- [SIEM operations](server/siem/operations.md): operating SIEM event delivery.
- [SIEM SecOps guide](server/siem/secops-guide.md): setting up the Google SecOps side of SIEM delivery.
- [SIEM curl runbook](server/siem/curl-runbook.md): checking SIEM delivery by hand with curl.
- [SIEM event catalog](server/siem/event-catalog.md): the events CIDX delivers to the SIEM.

## Reference

- [CLI reference](reference/cli/README.md): every `cidx` command and option (generated).
- [MCP tool catalog](reference/mcp-tools/README.md): every MCP tool by category, with parameters and permissions (generated).
- [Error codes](reference/error-codes/README.md): the server's error-code registry (generated).
- MCP tools: one document per tool in [src/code_indexer/server/mcp/tool_docs/](../src/code_indexer/server/mcp/tool_docs/).

## Architecture

- [Overview](architecture/overview.md): system design and storage architecture.
- [Invariants](architecture/invariants/README.md): rules the code must keep, one file per topic.
- [Indexing](architecture/indexing.md): file discovery, chunking, embedding batches, deduplication and publishing.
- [Storage](architecture/storage.md): chunk layouts (JSON shards and chunks.db), migration, HNSW integrity, temporal location.
- [Refresh recovery](architecture/refresh-recovery.md): failed refresh classification, restore, backoff and deferred triggers.
- [Query path](architecture/query-path.md): query embedding cache, coalescing, provider calls and the REST/MCP seams.
- [Repository lifecycle](architecture/repository-lifecycle.md): golden clones, aliases, immutable snapshots, retention and activation.
- [Cluster](architecture/cluster.md): multi-node design on PostgreSQL and shared storage.
- [Dependency map](architecture/dependency-map.md): the cross-repository dependency map pipeline and the cidx-meta backup.
- [X-Ray architecture](architecture/xray/architecture.md): the X-Ray AST search engine.
- [X-Ray graph binder internals](architecture/xray/graph-binder-internals.md): how X-Ray graph mode binds symbols.
- [X-Ray sandbox](architecture/xray/sandbox.md): how user evaluators are compiled and isolated.
- [X-Ray templates](xray-templates/): evaluator templates compiled into the X-Ray binary (Rust sources, not prose).

## Architecture decision records

- [ADR-001: X-Ray evaluator execution modes](adr/ADR-001-xray-evaluator-execution-modes.md)
- [ADR-002: X-Ray graph handle FFI](adr/ADR-002-xray-graph-handle-ffi.md)
- [ADR-003: graph memory governor integration](adr/ADR-003-graph-memory-governor-integration.md)

## Archive (historical, not maintained)

Kept for reference only; they describe past states of the system.

- [Migration to v8](archive/migration-to-v8.md): upgrading from 7.x.
- [Migration to v10](archive/migration-to-v10.md): upgrading from 9.x.
- [Query embedding cache empirical study](archive/query-embedding-cache-empirical-study.md): a point-in-time measurement study.
- [HNSW temporal orphans investigation](archive/hnsw-temporal-orphans-1330.md): a point-in-time research note on HNSW orphans.

## Elsewhere in the repository

- [CONTRIBUTING.md](../CONTRIBUTING.md): development setup, branches, test suites, releases.
- [SECURITY.md](../SECURITY.md): reporting vulnerabilities.
- [CHANGELOG.md](../CHANGELOG.md): release history.
