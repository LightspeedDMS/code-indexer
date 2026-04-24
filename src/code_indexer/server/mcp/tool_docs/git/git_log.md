---
name: git_log
category: git
required_permission: query_repos
tl_dr: Browse commit history with filtering by path, author, date, or branch.
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
