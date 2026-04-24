---
name: git_file_history
category: git
required_permission: query_repos
tl_dr: Get all commits that modified a specific file.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: Repository alias or full path.
    path:
      type: string
      description: 'Path to file (relative to repo root). Must be a file path, not directory. Examples: ''src/auth/login.py'',
        ''package.json'', ''docs/API.md''.'
    limit:
      type: integer
      description: 'Maximum commits to return. Default: 50. Range: 1-500. For files with long history, start with lower limits
        and use date filters to narrow results.'
      default: 50
      minimum: 1
      maximum: 500
    follow_renames:
      type: boolean
      description: 'Follow file history across renames. Default: true.'
      default: true
    rerank_query:
      type: string
      description: 'Query for cross-encoder reranking. When set, file history commits are semantically reranked before return. Leave empty to preserve the default chronological order.'
    rerank_instruction:
      type: string
      description: 'Optional instruction prefix for the reranker (e.g. ''Find commits that introduced bugs''). Has no effect without rerank_query. Steers ranking only; does not change which commits are included.'
  required:
  - repository_alias
  - path
outputSchema:
  type: object
  properties:
    success:
      type: boolean
    path:
      type: string
    commits:
      type: array
    total_count:
      type: integer
    truncated:
      type: boolean
    renamed_from:
      type:
      - string
      - 'null'
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
  required:
  - success
---

TL;DR: Get all commits that modified a specific file. WHEN TO USE: (1) Track file evolution, (2) Find when bug was introduced, (3) See who worked on a file. WHEN NOT TO USE: Repo-wide history -> git_log | Line attribution -> git_blame | View old version -> git_file_at_revision. RELATED TOOLS: git_log (repo-wide history, can also filter by path), git_blame (who wrote each line), git_file_at_revision (view file at commit).

### Reranking Parameters (Optional)

**Mental model — two-query pattern**: Use `path` to select which file's history to retrieve; use `rerank_query` (verbose natural language) to pick the best ordering from those commits. These serve different purposes.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing your ideal commit. The cross-encoder scores each commit's `subject` text against this description.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Prioritize commits that changed core business logic, not formatting or test updates". Has no effect without rerank_query.

#### When to Proactively Add Reranking

Consider adding rerank_query even when the user did not ask for it explicitly:
- The file has a long history and the user only cares about a specific kind of change
- The result set will likely be >5 commits where ordering matters
- Chronological order does not reflect what the user actually wants on top

#### When to Use Reranking

File history returns commits in reverse-chronological order by default. Cross-encoder reranking re-scores commits against your rerank_query to surface commits that best describe what you are looking for. Particularly useful when a file has many commits but only a few are semantically relevant to your intent.

Reranking for git_file_history uses the commit `subject` text. It works best when commit messages are reasonably descriptive. Very terse or auto-generated commit messages limit reranker usefulness.

#### What Reranking Does Not Do

Reranking does not bypass the file history query or search different branches. However, when `rerank_query` is set, the handler automatically overfetches up to 5x the requested `limit` (capped at 200) to give the reranker a larger candidate pool, then truncates back to `limit` after reranking.

#### Returned Telemetry

When reranking is requested, the response includes query_metadata with:
- reranker_used
- reranker_provider
- rerank_time_ms

If providers are disabled, unavailable, or all rerank attempts fail, the tool falls back to the base chronological ordering and reports that reranking was not used.

#### Examples

**With reranking — finding when a bug was introduced:**
```json
{
  "repository_alias": "backend-global",
  "path": "src/auth/session.py",
  "rerank_query": "commits that introduced or changed session expiration or token validation logic",
  "rerank_instruction": "Prioritize commits that modified authentication code, not test or config changes",
  "limit": 20
}
```

**With reranking — finding refactoring commits:**
```json
{
  "repository_alias": "backend-global",
  "path": "src/core/processor.py",
  "rerank_query": "commits that refactored or restructured the processing pipeline",
  "limit": 20
}
```

**Without reranking (intentional opt-out):**
```json
{
  "repository_alias": "backend-global",
  "path": "src/auth/session.py",
  "limit": 20
}
```
Result: same commits in reverse-chronological order, with no reranking overhead.
