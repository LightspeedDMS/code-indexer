---
name: git_search_diffs
category: git
required_permission: query_repos
tl_dr: Find when specific code was added/removed in git history (pickaxe search).
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: 'Repository identifier: either an alias (e.g., ''my-project'') or full path (e.g., ''/home/user/repos/my-project'').
        Use list_global_repos to see available repositories and their aliases.'
    search_string:
      type: string
      description: 'Exact string to search for in diff content. Finds commits where this string was added or removed. Use
        for function names, variable names, or specific code. Example: ''calculateTotalPrice''. Mutually exclusive with search_pattern.'
    search_pattern:
      type: string
      description: 'Regex pattern to search for in diff content. Finds commits where lines matching the pattern were added
        or removed. Use for flexible matching. Example: ''def\s+calculate.*'' to find function definitions. Mutually exclusive
        with search_string. Requires is_regex=true.'
    is_regex:
      type: boolean
      description: 'When true, use search_pattern as regex (-G flag). When false, use search_string as literal (-S flag).
        Default: false. Regex is slower but more flexible.'
      default: false
    path:
      type: string
      description: 'Limit search to diffs in this path (file or directory). Relative to repo root. Default: entire repository.
        Examples: ''src/auth/'', ''lib/utils.py''.'
    since:
      type: string
      description: 'Search only commits after this date. Format: YYYY-MM-DD or relative. Default: no limit. Useful to narrow
        down large search results.'
    until:
      type: string
      description: 'Search only commits before this date. Format: YYYY-MM-DD or relative. Default: no limit.'
    limit:
      type: integer
      description: 'Maximum number of matching commits to return. Default: 50. Range: 1-200. Diff search is computationally
        expensive; lower limits recommended. Response indicates if results were truncated.'
      default: 50
      minimum: 1
      maximum: 200
    rerank_query:
      type: string
      description: 'Query for cross-encoder reranking. When set, matching diff results are semantically reranked before return. Leave empty to preserve the default diff-match order.'
    rerank_instruction:
      type: string
      description: 'Optional instruction prefix for the reranker (e.g. ''Find commits that introduced bugs''). Has no effect without rerank_query. Steers ranking only; does not change which commits match.'
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    search_term:
      type: string
      description: Search term used
    is_regex:
      type: boolean
      description: Whether regex mode was used
    matches:
      type: array
      description: List of commits that added/removed matching content
      items:
        type: object
        properties:
          hash:
            type: string
            description: Full 40-char commit SHA
          short_hash:
            type: string
            description: Abbreviated SHA
          author_name:
            type: string
            description: Author name
          author_date:
            type: string
            description: Author date (ISO 8601)
          subject:
            type: string
            description: Commit subject line
          files_changed:
            type: array
            items:
              type: string
            description: Files modified in this commit
          diff_snippet:
            type:
            - string
            - 'null'
            description: Relevant portion of diff (if available)
    total_matches:
      type: integer
      description: Number of matching commits
    truncated:
      type: boolean
      description: Whether results were truncated
    search_time_ms:
      type: number
      description: Search execution time in ms
    query_metadata:
      type: object
      description: Reranking telemetry when rerank_query is provided
      properties:
        reranker_used:
          type: boolean
          description: Whether cross-encoder reranking was actually applied
        reranker_provider:
          type:
          - string
          - 'null'
          description: Provider that performed reranking ('voyage', 'cohere'), or null when reranking was not used
        rerank_time_ms:
          type: integer
          description: Time spent in the reranking stage in milliseconds
    error:
      type: string
      description: Error message if failed
  required:
  - success
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
