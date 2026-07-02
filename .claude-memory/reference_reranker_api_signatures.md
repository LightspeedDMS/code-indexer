---
name: Reranker API Signatures — Voyage rerank-2.5 and Cohere
description: Verified HTTP API parameters for both reranker providers — confirmed no separate instruction field in either
type: reference
---

## Verified: Voyage AI rerank-2.5 API

**Endpoint**: POST https://api.voyageai.com/v1/rerank

**HTTP body parameters** (confirmed via docs.voyageai.com):
```json
{
  "query": "string (required) — the search query",
  "documents": ["array of strings (required)"],
  "model": "rerank-2.5",
  "top_k": 10,
  "truncation": true
}
```

**NO separate `instruction` field in the API.** Instruction-following works by prepending the instruction to the query string before the call: `query = f"{instruction}\n{query_text}"`.

## Verified: Cohere rerank API

**Endpoint**: POST https://api.cohere.com/v2/rerank

**HTTP body parameters** (confirmed via docs.cohere.com):
```json
{
  "query": "string (required)",
  "documents": ["array of strings (required)"],
  "model": "rerank-v3.5",
  "top_n": 10,
  "max_tokens_per_doc": 4096
}
```

**NO instruction parameter at all.** Cohere reranker is query-only — no instruction steering support. Workaround: concatenate instruction + query: `f"{instruction} {rerank_query}"`.

## Design Implication

- Activation flag in `search_code`/`regex_search`/`git_search_commits`/`git_search_diffs`: `rerank_query` presence triggers reranking
- `rerank_instruction` is optional steering, prepended (Voyage: `\n` separator) or concatenated (Cohere: space separator) by the client before the API call
- Neither provider has a native instruction API field

*Verified 2026-04-07 during Epic #649 design session*
