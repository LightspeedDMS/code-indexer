---
name: git_search_commits
category: git
required_permission: query_repos
tl_dr: Search commit messages for keywords, ticket numbers, or patterns.
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: Repository alias or full path.
    query:
      type: string
      description: Text or pattern to search in commit messages. Case-insensitive by default.
    is_regex:
      type: boolean
      description: Treat query as regular expression (POSIX extended syntax).
      default: false
    author:
      type: string
      description: Filter by author name or email. Partial matches supported.
    since:
      type: string
      description: 'Commits after this date. Format: YYYY-MM-DD or relative (e.g., ''6 months ago'').'
    until:
      type: string
      description: 'Commits before this date. Format: YYYY-MM-DD or relative.'
    limit:
      type: integer
      description: Maximum matching commits to return.
      default: 50
      minimum: 1
      maximum: 500
    response_format:
      type: string
      description: 'Response format for multi-repo queries: flat (default) or grouped by repository'
      enum:
        - flat
        - grouped
      default: flat
    rerank_query:
      type: string
      description: 'Query for cross-encoder reranking. When set, matching commits are semantically reranked before return. Leave empty to preserve the default commit-match order.'
    rerank_instruction:
      type: string
      description: 'Optional instruction prefix for the reranker (e.g. ''Find bug fix commits''). Has no effect without rerank_query. Steers ranking only; does not change which commits match.'
  required:
  - repository_alias
  - query
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    query:
      type: string
      description: Search query used
    is_regex:
      type: boolean
      description: Whether regex mode was used
    matches:
      type: array
      description: List of matching commits
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
          author_email:
            type: string
            description: Author email
          author_date:
            type: string
            description: Author date (ISO 8601)
          subject:
            type: string
            description: Commit subject line
          body:
            type: string
            description: Full commit message body
          match_highlights:
            type: array
            items:
              type: string
            description: Lines containing matches
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

TL;DR: Search commit messages for keywords, ticket numbers, or patterns. WHEN TO USE: (1) Find commits mentioning 'JIRA-123', (2) Search for 'fix bug', (3) Find feature-related commits by message. WHEN NOT TO USE: Find when code was added/removed -> git_search_diffs | Browse recent history -> git_log | Commit details -> git_show_commit. RELATED TOOLS: git_search_diffs (search code changes), git_show_commit (view commit), git_log (browse history).

### Reranking Parameters (Optional)

**rerank_query**: When provided, enables cross-encoder reranking to reorder results by semantic relevance.
This is DIFFERENT from query: query is a keyword or pattern matched against commit message text,
while rerank_query is optimized for cross-encoder scoring (verbose natural language descriptions work better).
Omit rerank_query to return results in the default commit-match order (no reranking overhead).

**rerank_instruction**: Optional relevance steering hint for the reranker. Has no effect without rerank_query.
It only influences ranking among the commits already matched. Example: "Prioritize commits that changed core business logic, not config or formatting".

#### When to Use Reranking

Commit message search can be noisy — short commit subjects, formatting conventions, and automated commit
messages create retrieval noise. Cross-encoder reranking re-scores commits against your rerank_query to
surface commits that best describe what you are looking for. Particularly useful when many commits match
the keyword but only a few are semantically relevant to your intent.

Reranking for git_search_commits uses the combined commit `subject + body` text. It works best when commit
messages are reasonably descriptive. Very terse or auto-generated commit messages limit reranker usefulness.

#### What Reranking Does Not Do

Reranking does NOT search additional commits. It only reorders the commits already matched by query or regex.

#### Returned Telemetry

When reranking is requested, the response includes query_metadata with:
- reranker_used
- reranker_provider
- rerank_time_ms

If providers are disabled, unavailable, or all rerank attempts fail, the tool falls back to the base commit
ordering and reports that reranking was not used.

#### Examples

**With reranking — finding session handling changes:**
```json
{
  "query": "authentication",
  "rerank_query": "commits that changed login or session handling logic, not configuration or documentation updates",
  "rerank_instruction": "Prioritize commits that modified authentication code, not test or config changes",
  "repository_alias": "backend-global",
  "limit": 20
}
```

**With reranking — finding bug fix commits:**
```json
{
  "query": "fix",
  "rerank_query": "commits that fixed a runtime error, null pointer, or data corruption bug in production code",
  "repository_alias": "backend-global",
  "limit": 20
}
```

**With reranking — regex commit search:**
```json
{
  "query": "fix|bug|incident",
  "is_regex": true,
  "rerank_query": "commits that fixed a customer-facing production issue in authentication or session handling",
  "repository_alias": "backend-global",
  "limit": 20
}
```

**Without reranking (intentional opt-out):**
```json
{
  "query": "authentication",
  "repository_alias": "backend-global",
  "limit": 20
}
```
Result: same commits in the default commit-match order, with no reranking overhead.
