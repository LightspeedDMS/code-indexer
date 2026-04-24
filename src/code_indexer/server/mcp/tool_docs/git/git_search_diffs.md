---
name: git_search_diffs
category: git
required_permission: query_repos
tl_dr: Find when specific code was added/removed in git history (pickaxe search).
---

TL;DR: Find when specific code was added/removed in git history (pickaxe search). WHAT IS PICKAXE? Git's term for searching code CHANGES (not commit messages). Finds commits where text was introduced or deleted. WHEN TO USE: (1) 'When was this function added?', (2) 'Who introduced this bug?', (3) Track code pattern evolution. WHEN NOT TO USE: Search commit messages -> use git_search_commits instead. WARNING: Can be slow on large repos (may take 1-3+ minutes). Start with limit=5. RELATED TOOLS: git_search_commits (searches commit messages), git_blame (who wrote current code), git_show_commit (view commit details).

### Reranking Parameters (Optional)

**Mental model — two-query pattern**: Use `search_string`/`search_pattern` (literal text or regex) to find commits where code was added or removed; use `rerank_query` (verbose natural language) to pick the best ordering from those matches. These serve different purposes.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing the ideal change. The cross-encoder scores each `diff_snippet` (or commit subject if no snippet) against this description.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Focus on commits that introduced a vulnerability, not refactors or renames". Has no effect without rerank_query.

#### When to Proactively Add Reranking

Consider adding rerank_query even when the user did not ask for it explicitly:
- The search string is common and matches many commits, but the user only cares about a specific kind of change
- The result set will likely be >5 diff matches where ordering matters
- Chronological or diff-position order does not reflect what the user actually wants on top

#### When to Use Reranking

Diff search retrieves code changes that contain your search string or pattern, but diff format noise
(hunk headers, +/- markers, context lines) can affect ordering. Cross-encoder reranking re-scores diff
snippets for semantic relevance to your rerank_query, cutting through formatting artifacts. Particularly
useful when a string appears in many commits but only a few represent the meaningful change you are
looking for.

Reranking for git_search_diffs uses `diff_snippet` when available, with fallback to the commit subject if a
snippet is unavailable. Because it only sees the returned snippet, omitted diff context can limit ranking quality.

#### What Reranking Does Not Do

Reranking does NOT search more history or find additional pickaxe matches. It only reorders the matched diff results.

#### When Not to Use Reranking

Skip reranking when raw historical order matters more than semantic best match, for example when you are tracing
first introduction, reviewing change chronology, or auditing all matching commits.

#### Returned Telemetry

When reranking is requested, the response includes query_metadata with:
- reranker_used
- reranker_provider
- rerank_time_ms

If providers are disabled, unavailable, or all rerank attempts fail, the tool falls back to the base diff
ordering and reports that reranking was not used.

#### Examples

**With reranking — finding security vulnerability fixes:**
```json
{
  "search_string": "security fix",
  "rerank_query": "code changes that fix security vulnerabilities such as injection, authentication bypass, or privilege escalation",
  "rerank_instruction": "Focus on commits that removed or replaced vulnerable code, not documentation updates",
  "repository_alias": "backend-global",
  "limit": 20
}
```

**With reranking — finding when a function was introduced:**
```json
{
  "search_string": "calculateTotalPrice",
  "rerank_query": "commit that first introduced the calculateTotalPrice function implementation",
  "repository_alias": "backend-global",
  "limit": 10
}
```

**With reranking — regex diff search:**
```json
{
  "search_pattern": "def\\s+authenticate|create_session",
  "is_regex": true,
  "rerank_query": "code changes that introduced or significantly modified production authentication flow",
  "repository_alias": "backend-global",
  "limit": 20
}
```

**Without reranking (intentional opt-out):**
```json
{
  "search_string": "security fix",
  "repository_alias": "backend-global",
  "limit": 20
}
```
Result: same commits in the default diff-match order, with no reranking overhead.
