---
name: cidx_quick_reference
category: guides
required_permission: query_repos
tl_dr: Get quick reference for CIDX MCP tools with decision guidance.
quick_reference: true
---

TL;DR: Get quick reference for CIDX MCP tools with decision guidance.

FIRST THING TO KNOW - REPOSITORY DISCOVERY:

CRITICAL: If you don't know which repository contains the code you're looking for, DO NOT GUESS. Follow this workflow:

1. Search cidx-meta-global FIRST:
   search_code(query_text='your topic', repository_alias='cidx-meta-global', limit=5)
   This returns .md files describing what each repository contains.

2. Read the returned .md file to identify the relevant repository.

3. THEN search the identified repository for actual code:
   search_code(query_text='your topic', repository_alias='identified-repo-global')

WHY THIS MATTERS: cidx-meta-global is the "index of indexes" - it contains descriptions of all repositories. Searching it first prevents wasted time searching wrong repos.

WORKFLOW DECISION TREE:

Q: Do you know which repository to search?
  YES -> Use single-repo search: search_code('topic', repository_alias='specific-repo-global')
  NO -> Search cidx-meta-global FIRST (see above)

Q: Need to compare across multiple repositories?
  YES -> Use multi-repo search with per_repo mode (see MULTI-REPO section below)

Q: Need best matches regardless of source?
  YES -> Use multi-repo search with global mode (see MULTI-REPO section below)

SINGLE-REPO SEARCH:
search_code(query_text='authentication', repository_alias='backend-global', limit=5)
- Use repository_alias as a string
- Best for deep-diving into one codebase

MULTI-REPO SEARCH:
search_code(query_text='authentication', repository_alias=['repo1-global', 'repo2-global'], aggregation_mode='per_repo', limit=10)

TOOLS SUPPORTING MULTI-REPO: search_code, regex_search, git_log, git_search_commits, list_files

AGGREGATION MODES:
- 'global': Returns top N results by score across all repos (best for discovery)
- 'per_repo': Distributes N evenly across repos (best for comparison)

LIMIT MATH (IMPORTANT):
limit=10 with 3 repos in per_repo mode returns 4+3+3=10 TOTAL results, NOT 30.
In global mode, returns top 10 by score regardless of source.

CACHING:
Large results (>2000 chars) return preview + cache_handle.
Each result has its own handle (not per-repo).
Use get_cached_content(handle) to fetch full content.

ERROR HANDLING:
Partial results supported. Failed repos appear in 'errors' field, successful repos return results.

PERFORMANCE TIP:
Start with limit=3-5 for multi-repo searches. Token usage scales with number of repos.

TOOL CATEGORIES:
search, scip, git_exploration, git_operations, files, repo_management, golden_repos, system, user_management, ssh_keys, meta

Use category filter to narrow results:
cidx_quick_reference(category='search') -> returns search tools with summaries
