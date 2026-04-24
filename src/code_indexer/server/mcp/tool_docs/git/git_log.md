---
name: git_log
category: git
required_permission: query_repos
tl_dr: Browse commit history with filtering by path, author, date, or branch.
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
    limit:
      type: integer
      description: Maximum commits to return.
      default: 50
      minimum: 1
      maximum: 500
    offset:
      type: integer
      description: Commits to skip for pagination.
      default: 0
      minimum: 0
    path:
      type: string
      description: Filter commits affecting this path (file or directory, relative to repo root).
    author:
      type: string
      description: Filter by author name or email. Partial matches supported.
    since:
      type: string
      description: 'Commits after this date. Format: YYYY-MM-DD or relative (e.g., ''2 weeks ago'').'
    until:
      type: string
      description: 'Commits before this date. Format: YYYY-MM-DD or relative (e.g., ''yesterday'').'
    branch:
      type: string
      description: 'Branch or tag to get log from. Default: current HEAD.'
    response_format:
      type: string
      description: 'Response format for multi-repo queries: flat (default) or grouped by repository'
      enum:
        - flat
        - grouped
      default: flat
    aggregation_mode:
      type: string
      description: 'Result aggregation mode for multi-repo queries: global (merge all) or per_repo (group by repository)'
      enum:
        - global
        - per_repo
    rerank_query:
      type: string
      description: 'Query for cross-encoder reranking. When set, commits are semantically reranked before return. Leave empty to preserve the default chronological order.'
    rerank_instruction:
      type: string
      description: 'Optional instruction prefix for the reranker (e.g. ''Find commits related to database migrations''). Has no effect without rerank_query. Steers ranking only; does not change which commits are included.'
  required:
  - repository_alias
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    commits:
      type: array
      description: List of commits matching filters
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
          committer_name:
            type: string
            description: Committer name
          committer_email:
            type: string
            description: Committer email
          committer_date:
            type: string
            description: Committer date (ISO 8601)
          subject:
            type: string
            description: Commit subject line
          body:
            type: string
            description: Full commit message body
    total_count:
      type: integer
      description: Number of commits returned
    truncated:
      type: boolean
      description: Whether results were truncated
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

TL;DR: Browse commit history with filtering by path, author, date, or branch. WHEN TO USE: (1) View recent commits, (2) Find when changes were made, (3) Filter history by author/date/path. WHEN NOT TO USE: Search commit messages for keywords -> git_search_commits | Find when code was added/removed -> git_search_diffs | Single commit details -> git_show_commit. RELATED TOOLS: git_show_commit (commit details), git_search_commits (search messages), git_diff (compare revisions).

### Reranking Parameters (Optional)

**Mental model -- two-query pattern**: Use filter parameters (`path`, `author`, `since`, `until`, `branch`) to select which commits to retrieve; use `rerank_query` (verbose natural language) to pick the best ordering from those commits. These serve different purposes.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing your ideal commit. The cross-encoder scores each commit's `subject` and `body` text against this description.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Prioritize commits that changed core business logic, not formatting or test updates". Has no effect without rerank_query.

#### When to Proactively Add Reranking

Consider adding rerank_query even when the user did not ask for it explicitly:
- The commit history is long and the user only cares about a specific kind of change
- The result set will likely be >5 commits where ordering matters
- Chronological order does not reflect what the user actually wants on top

#### When to Use Reranking

Commit history returns commits in reverse-chronological order by default. Cross-encoder reranking re-scores commits against your rerank_query to surface commits that best describe what you are looking for. Particularly useful when a repository has many commits but only a few are semantically relevant to your intent.

Reranking for git_log uses the commit `subject` and `body` text. It works best when commit messages are reasonably descriptive. Very terse or auto-generated commit messages limit reranker usefulness.

#### What Reranking Does Not Do

Reranking does not bypass the commit log query or search different branches. However, when `rerank_query` is set, the handler automatically overfetches up to 5x the requested `limit` (capped at 200) to give the reranker a larger candidate pool, then truncates back to `limit` after reranking.

#### Returned Telemetry

When reranking is requested, the response includes query_metadata with:
- reranker_used
- reranker_provider
- rerank_time_ms

If providers are disabled, unavailable, or all rerank attempts fail, the tool falls back to the base chronological ordering and reports that reranking was not used.

#### Examples

**With reranking -- finding database migration commits:**
```json
{
  "repository_alias": "backend-global",
  "rerank_query": "commits that introduced or changed database migration logic and schema changes",
  "rerank_instruction": "Prioritize commits that modified database schemas, not test or documentation changes",
  "limit": 20
}
```

**With reranking -- filtering by author and reranking by topic:**
```json
{
  "repository_alias": "backend-global",
  "author": "dev@example.com",
  "rerank_query": "commits related to authentication and session management",
  "limit": 20
}
```

**Without reranking (intentional opt-out):**
```json
{
  "repository_alias": "backend-global",
  "limit": 20
}
```
Result: same commits in reverse-chronological order, with no reranking overhead.
