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
      description: 'Query for cross-encoder reranking. When set, results are reranked by relevance before return. Leave empty for keyword-match order.'
    rerank_instruction:
      type: string
      description: 'Instruction prefix for the reranker (e.g. ''Find bug fix commits''). Has no effect without rerank_query.'
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
Omit rerank_query to return results in keyword-match order (no reranking overhead).

**rerank_instruction**: Optional relevance steering hint passed to the Voyage AI reranker. Has no effect
without rerank_query or when using the Cohere reranker (which receives the instruction concatenated into
the query). Example: "Prioritize commits that changed core business logic, not config or formatting".

#### When to Use Reranking

Commit message search can be noisy — short commit subjects, formatting conventions, and automated commit
messages create retrieval noise. Cross-encoder reranking re-scores commits against your rerank_query to
surface commits that best describe what you are looking for. Particularly useful when many commits match
the keyword but only a few are semantically relevant to your intent.

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

**Without reranking (intentional opt-out):**
```json
{
  "query": "authentication",
  "repository_alias": "backend-global",
  "limit": 20
}
```
Result: same commits but in keyword-match order (most recent or relevance by git), with no reranking overhead.
