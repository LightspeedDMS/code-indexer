---
name: regex_search
category: search
required_permission: query_repos
tl_dr: Exhaustive regex pattern search on repository files without using indexes.
---

Exhaustive regex pattern search on repository files without using indexes. Slower than search_code but guarantees finding ALL matches.

KEY DIFFERENCE: regex_search searches files directly (comprehensive, slower) vs search_code FTS mode which uses indexes (fast, approximate). Use regex_search when you need guaranteed complete results.

EXAMPLE: regex_search(repository_alias='backend-global', pattern='def authenticate')

### Reranking Parameters (Optional)

**Mental model — two-query pattern**: Use `pattern` (exact regex) to find matching lines; use `rerank_query` (verbose natural language) to pick the best ordering from those matches. These serve different purposes.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing your ideal match. The cross-encoder scores each `line_content` against this description.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Focus on production authentication code, not test stubs". Has no effect without rerank_query.

#### When to Proactively Add Reranking

Consider adding rerank_query even when the user did not ask for it explicitly:
- The pattern is broad and matches many files, but the user only cares about a subset
- The result set will likely be >5 matches where ordering matters
- File-path or match-position ordering does not reflect what the user actually wants on top

#### When to Use Reranking

Regex results have NO semantic ordering — results are ordered by file path or match position, not by
relevance. Cross-encoder reranking adds semantic relevance scoring on top of regex pattern matching,
ensuring the most semantically relevant matches appear first. This is especially valuable when a pattern
matches many files but only a subset are actually relevant to your intent.

Reranking for regex_search is based on each match's `line_content`, not the full file. It works best when
the matching line carries meaningful context. Very short or ambiguous match lines may rerank poorly.

#### What Reranking Does Not Do

Reranking does NOT find additional regex matches. It only reorders the matches already returned by the
pattern search.

#### When Not to Use Reranking

Skip reranking when doing exhaustive auditing or compliance-style searches where completeness matters but
semantic prioritization does not. It also adds latency for broad searches with many matches.

#### Returned Telemetry

When reranking is requested, the response includes query_metadata with:
- reranker_used
- reranker_provider
- rerank_time_ms

If reranking is requested but providers are disabled, unavailable, or all attempts fail, the tool returns
the base regex ordering and reports that reranking was not used.

#### Examples

**With reranking — finding auth function definitions:**
```json
{
  "pattern": "def.*auth",
  "rerank_query": "authentication and authorization logic that validates user identity or access rights",
  "rerank_instruction": "Focus on production code, not test fixtures or mock helpers",
  "repository_alias": "backend-global",
  "max_results": 20
}
```

**With reranking — broad pattern narrowed semantically:**
```json
{
  "pattern": "auth|token|session",
  "rerank_query": "production authentication code that validates tokens or creates authenticated sessions",
  "repository_alias": "backend-global",
  "max_results": 30
}
```

**With reranking — finding error handler patterns:**
```json
{
  "pattern": "except.*Exception",
  "rerank_query": "exception handlers that log errors and return meaningful error responses to callers",
  "repository_alias": "backend-global",
  "max_results": 20
}
```

**Without reranking (intentional opt-out):**
```json
{
  "pattern": "def.*auth",
  "repository_alias": "backend-global",
  "max_results": 20
}
```
Result: same matches but in file path / match-position order, with no reranking overhead.
