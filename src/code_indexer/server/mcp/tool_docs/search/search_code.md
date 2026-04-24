---
name: search_code
category: search
required_permission: query_repos
tl_dr: 'MANDATORY REPOSITORY DISCOVERY - READ THIS FIRST:

  If user does NOT explicitly specify a repository, you MUST:

  1.'
---

MANDATORY REPOSITORY DISCOVERY - READ THIS FIRST:
If user does NOT explicitly specify a repository, you MUST:
1. ALWAYS search cidx-meta-global FIRST: search_code('topic', repository_alias='cidx-meta-global', limit=5)
2. cidx-meta-global contains AI-generated descriptions of every other repository on this server
3. INTERPRETING RESULTS: file_path='auth-service.md' means auth-service-global is relevant. Rule: strip '.md', append '-global' to get the searchable alias
4. DEPENDENCY MAP RESULTS: file_path='dependency-map/authentication.md' describes cross-repo relationships in that domain. These results are especially valuable when the topic spans multiple repos or you need architectural context. Read the snippet -- it lists participating repos, their roles, and how they interact. Then search those repos together using repository_alias as an array
5. THEN search the identified repo(s) for actual code: search_code('topic', repository_alias='auth-service-global', limit=10)
6. IF cidx-meta-global NOT FOUND: fall back to list_global_repos() and search candidates directly

Skip discovery ONLY when user explicitly names a repository (e.g., "search in backend-global").

REPOSITORY SELECTION:
1. User specified exact repo? -> Search directly
2. User mentioned topic WITHOUT repo? -> cidx-meta-global discovery (MANDATORY)
3. Cross-repo comparison? -> repository_alias as array + aggregation_mode='per_repo'
4. Best matches anywhere? -> repository_alias as array + aggregation_mode='global'

SEARCH MODE: 'authentication logic' (concept) -> semantic | 'def authenticate_user' (exact) -> fts | unsure -> hybrid (runs both, merges via RRF - common hits ranked highest)

RERANKING SMELL TEST: query_text is 2+ words and search_mode is semantic or hybrid? Add rerank_query. Write query_text short for retrieval, write rerank_query long for precision. Skipping reranking on a conceptual query typically costs 2-4 additional searches to find the right result; reranking adds ~200-500ms but is almost always cheaper than re-searching.

SKIP RERANKING ONLY WHEN: exact single-identifier lookup (e.g., fts for 'def authenticate_user') | result set <= 3 | chronological/positional order matters more than relevance

STANDARD CALL TEMPLATE:
  query_text:          [short: 1-4 keywords for retrieval]
  search_mode:         [semantic|fts|hybrid]
  repository_alias:    [target repo]
  limit:               [start at 5]
  rerank_query:        [FILL THIS IN — verbose sentence describing your ideal result]
  rerank_instruction:  [FILL THIS IN — what to deprioritize, e.g., "Focus on implementation, not tests"]

CRITICAL: Semantic search finds code by MEANING, not exact text. Results are APPROXIMATE. For exhaustive exact-text results, use FTS mode or regex_search tool.

LIMIT BEHAVIOR: limit=10 with 3 repos in 'global' mode may return 7+3+0=10. In 'per_repo' mode returns 4+3+3=10 (NOT 30 - per_repo does NOT multiply the limit).

PERFORMANCE: Start with limit=5. Each result consumes tokens proportional to code snippet size. Large fields may be truncated to snippet_preview + snippet_cache_handle (use get_cached_content to retrieve full content).

EXAMPLE (standard call with reranking):
search_code('authentication logic', repository_alias='backend-global', search_mode='semantic', limit=5,
            rerank_query='production code implementing user authentication flow',
            rerank_instruction='Focus on implementation, not tests or config')

### Reranking

DEFAULT: Use reranking. Skip it only for exact identifier lookups, result sets <= 3, or when retrieval order already matches intent.

**Mental model — two-query pattern**: Use `query_text` (short, 1-4 keywords) to retrieve candidates; use `rerank_query` (verbose natural language sentence) to pick the best ordering. These serve different purposes and should be written differently.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing your ideal result. The cross-encoder reads both this and each result snippet to score relevance. Cost: ~200-500ms latency. Benefit: saves 2-4 follow-up searches when the query is conceptual.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Focus on production implementation, not test fixtures or mock helpers". Has no effect without rerank_query.

Add rerank_query proactively (even without explicit user request) when:
- The user's intent is conceptual or multi-faceted (not a simple identifier lookup)
- The result set will likely be >5 candidates where ordering matters
- The base retrieval order does not match what the user actually wants on top

What reranking does NOT do: Reranking only reorders the candidate set already retrieved. It cannot recover a file the base query missed. Quality depends on the initial candidate pool.

Returned telemetry: reranker_used, reranker_provider, rerank_time_ms. If providers are unavailable or all attempts fail, the tool falls back to base retrieval order.

#### Examples

**Finding login implementation:**
```json
{
  "query_text": "login authentication",
  "rerank_query": "function that validates user credentials and creates a session on successful login",
  "rerank_instruction": "Focus on implementation, not test fixtures",
  "repository_alias": "backend-global",
  "limit": 10
}
```

**Hybrid/FTS with semantic prioritization:**
```json
{
  "query_text": "authenticate session token",
  "search_mode": "hybrid",
  "rerank_query": "production code that validates a session token and rejects expired or malformed credentials",
  "repository_alias": "backend-global",
  "limit": 10
}
```

**Intentional opt-out (exact identifier lookup):**
```json
{
  "query_text": "authenticate_user",
  "search_mode": "fts",
  "repository_alias": "backend-global",
  "limit": 10
}
```
