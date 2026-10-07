# Embedding Providers and Reranking

The two embedding providers CIDX supports, VoyageAI and Cohere: their models and vector dimensions, where each
reads its API key, how one repository can carry indexes from both, how results are reranked, and how the server
tracks provider health.

Audience: CLI users choosing a provider, and server operators running indexes from two providers. Search options
themselves are in the [Query guide](query.md); the server's internal query pipeline (embedding cache, coalescer,
concurrency governor) is in [Query path](../architecture/query-path.md).

## Contents

- [Providers and models](#providers-and-models)
- [API keys](#api-keys)
- [Multi-provider indexes](#multi-provider-indexes)
- [Query strategy and score fusion](#query-strategy-and-score-fusion)
- [Reranking](#reranking)
- [Provider health](#provider-health)
- [Troubleshooting](#troubleshooting)

## Providers and models

| Provider id | Default model | Dimensions of the default | Key |
|-------------|---------------|---------------------------|-----|
| `voyage-ai` (default) | `voyage-code-3` | 1024 | `VOYAGE_API_KEY` |
| `cohere` | `embed-v4.0` | 1536 | `CO_API_KEY` |

A project picks its provider with `embedding_provider` in `.code-indexer/config.json` and its model with
`voyage_ai.model` or `cohere.model` (see [Configuration](../getting-started/configuration.md)).

Vector dimensions the VoyageAI client expects per model (`_VOYAGE_MODEL_DIMENSIONS` in
`src/code_indexer/services/voyage_ai.py`; other models default to 1024):

| VoyageAI model | Dimensions |
|----------------|------------|
| `voyage-code-3` | 1024 |
| `voyage-large-2` | 1536 |
| `voyage-code-2` | 1536 |
| `voyage-2` | 1024 |
| `voyage-law-2` | 1024 |
| `voyage-multimodal-3` | 1024 |
| `voyage-context-4` | 1024 |

Cohere's `embed-v4.0` uses its `default_dimension`, 1536, from `src/code_indexer/data/cohere_models.yaml`. Token
limits and request sizes per model, used to size embedding batches, are in
`src/code_indexer/data/voyage_models.yaml` and `cohere_models.yaml`.

`voyage-context-4` and `embed-v4.0` are also the embedders of the per-commit temporal index; see
[Temporal search](temporal-search.md).

Each model gets its own collection directory, `.code-indexer/index/<model>/` (the model name, with `/` and `:`
replaced by `_`; `resolve_collection_name` in `src/code_indexer/storage/filesystem_vector_store.py`). Changing the
model therefore builds a new collection beside the old one rather than mixing vectors of different sizes.

## API keys

**CLI.** VoyageAI reads `VOYAGE_API_KEY` from the environment only. Cohere uses `cohere.api_key` from `config.json`
when it is non-empty, otherwise `CO_API_KEY`. The CLI does not read `.env` files. Details and examples:
[Configuration](../getting-started/configuration.md#embedding-provider-keys).

**Server.** Administrators store the VoyageAI and Cohere keys in the Web UI (runtime settings
`claude_integration_config.voyageai_api_key` and `cohere_api_key`; REST `/api/api-keys/...`). The server copies a
configured key into its own process environment (`VOYAGE_API_KEY`, `CO_API_KEY`) at startup and again when the
setting changes, and indexing subprocesses inherit it (`seed_api_keys_on_startup` in
`src/code_indexer/server/startup/api_key_seeding.py`). Precedence:

| Web UI setting | Environment of the server process | Key used |
|----------------|-----------------------------------|----------|
| set | anything | the Web UI value (it overwrites the environment variable) |
| blank | variable set when the server started | the environment value (left untouched) |
| blank | not set | none: that provider is unavailable |

## Multi-provider indexes

A repository can hold one index per provider. Each provider's vectors live in their own collection (one directory
per model), so adding or removing one provider never touches the other.

**CLI.** List the providers in `config.json`:

```json
{"embedding_provider": "voyage-ai", "embedding_providers": ["voyage-ai", "cohere"]}
```

`cidx index` then indexes with every listed provider that has an API key and skips the others with a warning; the
first provider with a key is treated as primary for that run. Each provider keeps its own incremental state,
`.code-indexer/metadata-<provider>.json`. A local `cidx query` still embeds the query with `embedding_provider` only
and does not fall back to the other provider (see
[Multi-provider query strategy](query.md#multi-provider-query-strategy)).

**Server.** When a golden repository is registered, the server writes `embedding_providers` into its
`config.json`: always `voyage-ai` first, even when the server has no VoyageAI key, followed by every other provider
that has a key (`_write_embedding_providers_to_config` in
`src/code_indexer/server/repositories/golden_repo_manager.py`). Administrators manage per-provider indexes
afterwards:

| Action | MCP | REST (`/api/admin/provider-indexes`) | Elevation |
|--------|-----|--------------------------------------|-----------|
| list providers that have a key | `manage_provider_indexes` `list_providers` | `GET /providers` | no |
| per-provider status of a repository | `manage_provider_indexes` `status` | `GET /status` | no |
| build a provider's index (background job) | `manage_provider_indexes` `add` | `POST /add` (202) | yes |
| rebuild a provider's index from scratch | `manage_provider_indexes` `recreate` | `POST /recreate` (202) | yes |
| delete a provider's collection | `manage_provider_indexes` `remove` | `POST /remove` | yes |
| add a provider's index to every golden repository that lacks it | `bulk_add_provider_index` | `POST /bulk-add` (202) | yes |

All of them require the `admin` role. Elevation applies only when the server enforces it; see
[Login and elevation](../server/auth/login-and-elevation.md#step-up-elevation). `bulk_add_provider_index` honours
only a `category:<name>` filter; any other filter value is ignored and every eligible repository is processed.

## Query strategy and score fusion

With two providers indexed, the server's MCP `search_code` tool can query one provider, fail over from VoyageAI to
Cohere, or query both and fuse the lists (`query_strategy` = `primary_only`, `failover`, `parallel`, `specific`;
`score_fusion` = `rrf`, `multiply`, `average`). The defaults, the fields returned with fused results and the REST
behaviour are described in [Multi-provider query strategy](query.md#multi-provider-query-strategy). In `parallel`
mode the server skips a provider whose health status is `down` or that is sin-binned (see
[Provider health](#provider-health)); if every provider is skipped, the query fails instead of returning an empty
result.

## Reranking

A reranker re-orders retrieved candidates against a rerank query before the list is cut to the requested limit.
Both VoyageAI and Cohere offer rerankers; they use the same API keys as embeddings.

| | CLI (`cidx query`) | Server (REST `POST /api/query`, MCP `search_code`) |
|---|---|---|
| When it runs | by default: the search query is used as the rerank query (`auto_populate_rerank_query: true`) | only when the request carries `rerank_query` |
| Turning it off | `--rerank-query ""` (non-temporal queries), or `auto_populate_rerank_query: false`; for a temporal query (`--time-range`, `--time-range-all`) an empty `--rerank-query` counts as not given, so auto-populate still applies | omit `rerank_query` |
| Models | `voyage_reranker_model` `rerank-2.5`, `cohere_reranker_model` `rerank-v3.5` | `voyage_reranker_model` and `cohere_reranker_model` in the runtime setting `rerank_config`; both empty by default, which disables server reranking |
| Vendor order | VoyageAI, then Cohere (the CLI runs the server's chain, `_run_provider_chain`; the `preferred_vendor_order` key in `global.json` does not change the order) | VoyageAI, then Cohere, each only if its model is set |
| Candidates fetched | `overfetch_multiplier` x limit (default 5), capped at 200 candidates | the same |
| Where configured | per-user file `~/.config/cidx/global.json` (or `$CIDX_GLOBAL_CONFIG_PATH`, `$XDG_CONFIG_HOME/cidx/global.json`) | Web UI configuration |

If a vendor fails, the next one is tried; if all fail, results are returned in retrieval order. On the server,
reranking runs after the results of all providers and repositories have been fused, and before payload truncation
and access filtering (`_apply_reranking_sync` in `src/code_indexer/server/mcp/reranking.py`; order described in
[Query path](../architecture/query-path.md#fusion-reranking-and-filtering)). `rerank_instruction` /
`--rerank-instruction` is passed to the vendor with the rerank query.

The printed score stays the retrieval similarity score; only the order changes. Executed against a two-file
scratch project (`src/app.py` and `tests/test_app.py`):

```text
$ cidx query "password check implementation" --limit 2 --quiet
1. 0.706 src/app.py:1-4
2. 0.587 tests/test_app.py:1-5
$ cidx query "password check implementation" --rerank-query "unit test" --limit 2 --quiet
1. 0.587 tests/test_app.py:1-5
2. 0.706 src/app.py:1-4
```

(Freshness markers removed from the output.) CLI reranking options in detail: [Reranking](query.md#reranking).

## Provider health

The server records every embedding and rerank call per provider and derives a health status over a 60-minute
rolling window (`ProviderHealthMonitor` in `src/code_indexer/services/provider_health_monitor.py`):

| Status | Condition |
|--------|-----------|
| `down` | error rate above 50%, or 5 consecutive failures |
| `degraded` | error rate above 10%, p95 latency above 5000 ms, or availability below 95% |
| `healthy` | otherwise |

The health score is availability reduced by a latency penalty (0.0 to 1.0). Separately, a provider that fails
repeatedly is sin-binned (taken out of rotation) with exponential backoff. The runtime settings `voyage_ai_sinbin`
and `cohere_sinbin` control it; defaults: 5 failures within 60 seconds, first cooldown 30 seconds, doubling up to
300 seconds (`ProviderSinBinConfig` in `src/code_indexer/server/utils/config_manager.py`).

Reading health (admin role):

- MCP `get_provider_health` (optionally `provider`),
- REST `GET /admin/provider-health` or `GET /api/admin/provider-indexes/health`.

The result carries p50/p95/p99 latency, error rate, availability, health score and status per provider. Health
and sin-bin state are held in memory by each server process: each worker and each cluster node keeps its own view,
and a restart clears it. `POST /admin/provider-health/clear-sinbin` (body `{"target": "<provider>"}`, or no target
for all) and `POST /admin/provider-health/reset-state` exist for test isolation and need the admin role and, when
enforced, elevation; they act on the process that handles the request.

The CLI and the daemon use the same monitor for rerankers, with the state saved between runs in
`$XDG_CONFIG_HOME/cidx/reranker_state.json` (default `~/.config/cidx/reranker_state.json`;
`_get_reranker_sinbin_path` in `src/code_indexer/cli.py`). A sin-binned reranker is skipped in favour of the next
vendor until its cooldown ends.

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `VOYAGE_API_KEY environment variable is required for VoyageAI.` | Export the key in the shell (CLI) or set it in the Web UI (server). |
| `Cohere API key required. Set via config or CO_API_KEY env var.` | Set `CO_API_KEY` or `cohere.api_key`. |
| `cidx index` warns that a provider is skipped | That provider in `embedding_providers` has no key; the others are indexed. |
| Server results are never reranked | `rerank_config` has no reranker model set, or the request has no `rerank_query`. |
| CLI order differs from the score order | Reranking is on by default; use `--rerank-query ""` (non-temporal queries) or `auto_populate_rerank_query: false` for similarity order. |
| A server query fails with providers unavailable | Every provider is `down` or sin-binned on that process; check `get_provider_health` and the provider's status page. |
