# Fault Injection and Memory Retrieval Invariants

Index of all invariant groups: [README](README.md).

## Fault injection harness

Operator guide: [Fault Injection](../../server/fault-injection.md).

- Non-production only and off by default. Both switches are bootstrap keys in `config.json`:
  `fault_injection_enabled` and `fault_injection_nonprod_ack` (both `false`).
- Enabled without the acknowledgement, or enabled in production, logs CRITICAL and exits with status 1
  (`src/code_indexer/server/fault_injection/startup.py`).
- All outbound HTTP to embedding and reranking providers goes through `HttpClientFactory`
  (`src/code_indexer/server/fault_injection/http_client_factory.py`). Direct `httpx` client construction outside the
  factory is caught by `tests/unit/server/fault_injection/test_http_client_factory.py`.
- With fault injection on, the factory ignores pooling and gives every call a fresh client with the fault-injecting
  transport, so every scripted fault intercepts every call. See
  [Server Runtime Invariants](server-runtime.md#server-memory-and-pooling) for the pooled production client.

## Memory retrieval

Operator guide: [Memory Retrieval](../../server/memory-retrieval.md).

- A parallel pipeline on semantic and hybrid `search_code` (`src/code_indexer/server/mcp/memory_retrieval_pipeline.py`,
  `handlers/search/memory_retrieval.py`): query vector, HNSW candidates, score floors, hydration, empty-state nudge.
- Kill switch: the runtime setting `memory_retrieval_enabled`, read on each request.
- Memory files are confined to the memories directory with `Path.relative_to()`; memory ids are validated.
- A candidate that cannot be hydrated is skipped with a WARNING; hydration never raises into the search.
