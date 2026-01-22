---
name: first_time_user_guide
category: guides
required_permission: null
tl_dr: Get step-by-step quick start guide for new CIDX MCP server users.
---

TL;DR: Get step-by-step quick start guide for new CIDX MCP server users. Shows the essential workflow to get started productively.

USE CASES:
(1) Brand new user who just connected to CIDX MCP server
(2) User who wants to verify they understand the basic workflow
(3) Onboarding team members to CIDX

WHAT YOU'LL LEARN:
- How to check your permissions and role
- How to discover available repositories
- How to run your first search
- How to explore repository structure
- How to activate repositories for file editing
- How to use git operations

THE WORKFLOW:
Step 1: Check your identity and permissions
  whoami() -> See your username, role, and what you can do

Step 2: Discover available repositories
  list_global_repos() -> See all repositories you can search

Step 3: Check repository capabilities
  global_repo_status('repo-name-global') -> Check what indexes exist (semantic, FTS, temporal, SCIP)

Step 4: Run your first search
  search_code(query_text='authentication', repository_alias='backend-global', limit=5)
  -> Find code related to authentication, start with small limit

Step 5: Explore repository structure
  browse_directory(repository_alias='backend-global', path='src')
  -> See what files and folders exist

Step 6: Use code intelligence (if SCIP available)
  scip_definition(symbol='authenticate_user', repository_alias='backend-global')
  -> Find where functions are defined

Step 7: For file editing - activate a repository
  activate_repository(username='yourname', golden_repo_alias='backend-golden', user_alias='my-backend')
  -> Creates your personal writable copy

Step 8: Make changes with git workflow
  create_file(...) -> edit_file(...) -> git_stage(...) -> git_commit(...) -> git_push(...)

ESSENTIAL WORKFLOWS:

WORKFLOW A - Unknown Repository (Discovery):
1. search_code('your topic', repository_alias='cidx-meta-global')
2. Read returned .md file to identify relevant repo
3. search_code('your topic', repository_alias='identified-repo-global')

WORKFLOW B - Cross-Cutting Analysis (Multi-Repo Search):
1. list_global_repos() to see available repos
2. Choose aggregation strategy:
   - aggregation_mode='per_repo': Distributes results evenly across repos (for comparison)
   - aggregation_mode='global': Returns best matches regardless of source (for discovery)
3. Run multi-repo search:
   search_code('topic', repository_alias=['repo1-global', 'repo2-global'], aggregation_mode='per_repo', limit=10)
4. Understand the results:
   - LIMIT BEHAVIOR: limit=10 with 3 repos returns 10 total (NOT 30). Per-repo mode distributes evenly.
   - CACHING: Large results return preview + cache_handle. Each result has its own handle.
   - ERRORS: Failed repos appear in 'errors' field, successful repos still return results.
5. For large results, fetch full content:
   get_cached_content(handle='uuid-from-result', page=0)

TOOLS SUPPORTING MULTI-REPO: search_code, regex_search, git_log, git_search_commits, list_files

WORKFLOW C - Deep Dive (Single Repo):
1. list_global_repos() to find repo name
2. search_code('topic', repository_alias='specific-repo-global')
3. get_file_content() to read full files
4. Use SCIP tools for code navigation

NEXT STEPS:
- Use get_tool_categories() to discover more tools
- Use cidx_quick_reference(category='search') for detailed tool selection guidance
- Check permission_reference if you get permission errors

COMMON QUESTIONS:
Q: When do I use '-global' suffix?
A: Always for global repos (read-only, shared). Never for activated repos (your personal copies).

Q: What's the difference between search_code and regex_search?
A: search_code uses pre-built indexes (fast, approximate). regex_search scans files directly (comprehensive, slower).

Q: Why can't I edit files in global repos?
A: Global repos are read-only. Activate them first to get your personal writable copy.

Q: How does limit work with multi-repo searches?
A: limit=10 with 3 repos returns 10 TOTAL results, not 30. In 'per_repo' mode, results are distributed evenly (4+3+3=10). In 'global' mode, top 10 by score regardless of source.

Q: What happens if one repo fails in a multi-repo search?
A: You get partial results. Successful repos return results normally, failed repos appear in 'errors' field.
