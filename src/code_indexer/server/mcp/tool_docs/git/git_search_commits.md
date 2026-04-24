---
name: git_search_commits
category: git
required_permission: query_repos
tl_dr: Search commit messages for keywords, ticket numbers, or patterns.
---

TL;DR: Search commit messages for keywords, ticket numbers, or patterns. WHEN TO USE: (1) Find commits mentioning 'JIRA-123', (2) Search for 'fix bug', (3) Find feature-related commits by message. WHEN NOT TO USE: Find when code was added/removed -> git_search_diffs | Browse recent history -> git_log | Commit details -> git_show_commit. RELATED TOOLS: git_search_diffs (search code changes), git_show_commit (view commit), git_log (browse history).

### Reranking Parameters (Optional)

**Mental model — two-query pattern**: Use `query` (short keyword or regex) to find matching commits; use `rerank_query` (verbose natural language) to pick the best ordering from those matches. These serve different purposes.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing your ideal commit. The cross-encoder scores each commit's `subject + body` text against this description.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Prioritize commits that changed core business logic, not config or formatting". Has no effect without rerank_query.

#### When to Proactively Add Reranking

Consider adding rerank_query even when the user did not ask for it explicitly:
- The keyword matches many commits but the user only cares about a specific kind of change
- The result set will likely be >5 commits where ordering matters
- Chronological or keyword-match order does not reflect what the user actually wants on top

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
