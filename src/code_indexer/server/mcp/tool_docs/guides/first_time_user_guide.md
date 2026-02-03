---
name: first_time_user_guide
category: guides
required_permission: null
tl_dr: Get step-by-step quick start guide for new CIDX MCP server users.
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    guide:
      type: object
      properties:
        steps:
          type: array
          items:
            type: object
            properties:
              step_number:
                type: integer
              title:
                type: string
              description:
                type: string
              example_call:
                type: string
              expected_result:
                type: string
        quick_start_summary:
          type: array
          items:
            type: string
          description: One-line summary of each step for quick reference
        common_errors:
          type: array
          items:
            type: object
            properties:
              error:
                type: string
              solution:
                type: string
---

TL;DR: Get step-by-step quick start guide for new CIDX MCP server users. Shows the essential workflow to get started productively.

USE CASES:
(1) Brand new user who just connected to CIDX MCP server
(2) User who wants to verify they understand the basic workflow
(3) Onboarding team members to CIDX

WHAT YOU'LL LEARN:
- How to check your permissions and role
- How to discover available repositories
- How to find which repository has the code you need (CRITICAL)
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
  global_repo_status(repository_alias='repo-name-global') -> Check what indexes exist (semantic, FTS, temporal, SCIP)

Step 4: CRITICAL - Find the right repository/repositories (Don't skip this!)

  IMPORTANT: If you don't know which repository contains the code you're looking for, DO NOT GUESS. Use cidx-meta-global first.

  cidx-meta-global is the "index of indexes" - it contains .md files describing what each repository contains.

  4a. Search cidx-meta-global to discover relevant repositories:
      search_code(query_text='authentication', repository_alias='cidx-meta-global', limit=5)
      -> Returns .md files describing which repos handle authentication

  4b. Read the returned .md files - your topic may exist in MULTIPLE repositories

  4c. Choose your next step based on results:
      - ONE relevant repo found -> Proceed to Step 5 (single-repo search)
      - MULTIPLE relevant repos found -> Proceed to WORKFLOW B (multi-repo search - RECOMMENDED)

  WHY THIS MATTERS: Skipping this step leads to wasted time searching wrong repositories. cidx-meta-global prevents guesswork and identifies ALL relevant codebases.

Step 5: Run your first search (in the correct repository)
  search_code(query_text='authentication', repository_alias='backend-global', limit=5)
  -> Find code related to authentication, start with small limit

Step 6: Explore repository structure
  browse_directory(repository_alias='backend-global', path='src')
  -> See what files and folders exist

Step 7: Use code intelligence (if SCIP available)
  scip_definition(symbol='authenticate_user', repository_alias='backend-global')
  -> Find where functions are defined

Step 8: For file editing - activate a repository
  activate_repository(username='yourname', golden_repo_alias='backend-golden', user_alias='my-backend')
  -> Creates your personal writable copy

Step 9: Make changes with git workflow
  create_file(...) -> edit_file(...) -> git_stage(...) -> git_commit(...) -> git_push(...)

ESSENTIAL WORKFLOWS:

WORKFLOW A - Unknown Repository (Discovery) - USE THIS FIRST:
This is the MOST COMMON workflow. Always start here if unsure which repo to search.
1. search_code(query_text='your topic', repository_alias='cidx-meta-global')
2. Read returned .md file(s) to identify relevant repo(s)
3. Based on results:
   - One repo found -> search_code(query_text='your topic', repository_alias='identified-repo-global')
   - Multiple repos found -> Use WORKFLOW B (RECOMMENDED)

WORKFLOW B - Cross-Repo Exploration (RECOMMENDED for multi-repo scenarios):

USE THIS WORKFLOW WHEN:
- cidx-meta-global returned MULTIPLE relevant repositories
- You need to explore/discover a concept across the codebase
- You want to compare implementations across repos

WHY MULTI-REPO IS PREFERRED OVER SEQUENTIAL SINGLE-REPO QUERIES:
- Single query instead of N sequential queries (faster, less overhead)
- Consistent scoring across all repositories (apples-to-apples comparison)
- Unified results with source_repo attribution (easy to see distribution)
- Parallel execution on server side (performance optimized)
- Partial failure handling (one repo down doesn't block others)

STEPS:
1. Identify target repos (from cidx-meta discovery or list_global_repos())
2. Choose aggregation strategy:
   - aggregation_mode='global' (default): Best matches by score (RECOMMENDED for discovery/exploration)
   - aggregation_mode='per_repo': Equal representation per repo (best for comparison)
3. Run multi-repo search:
   search_code(query_text='topic', repository_alias=['repo1-global', 'repo2-global'], aggregation_mode='global', limit=10)
4. Understand the results:
   - LIMIT BEHAVIOR: limit=10 with 3 repos returns 10 total (NOT 30). Per-repo mode distributes evenly.
   - CACHING: Large results return preview + cache_handle. Each result has its own handle.
   - ERRORS: Failed repos appear in 'errors' field, successful repos still return results.
5. For large results, fetch full content:
   get_cached_content(handle='uuid-from-result', page=0)

SYNTAX OPTIONS:
- Specific repos: repository_alias=['backend-global', 'frontend-global']
- Wildcard ALL: repository_alias='*-global' (searches all global repos)
- Pattern match: repository_alias='pch-*-global' (all repos matching pattern)

TOOLS SUPPORTING MULTI-REPO: search_code, regex_search, git_log, git_search_commits, list_files

WORKFLOW C - Deep Dive (Single Repo):
Use when you KNOW which repo to search (or cidx-meta found only ONE relevant repo).
1. list_global_repos() to find repo name
2. search_code(query_text='topic', repository_alias='specific-repo-global')
3. get_file_content() to read full files
4. Use SCIP tools for code navigation

WORKFLOW D - Research Session Tracing (Optional Observability):
Use when you want to track tool usage, measure performance, or debug issues.

WHY USE TRACING:
- Track which searches led to successful code discovery
- Measure query performance and optimize search patterns
- Create audit trails for compliance or debugging
- Analyze usage patterns across research sessions

STEPS:
1. Start a trace at the beginning of your session:
   start_trace(name='auth-research', metadata={'goal': 'find login flow'})

2. Execute your normal workflow (searches, file reads, SCIP queries):
   search_code(query_text='authentication', repository_alias='backend-global')
   get_file_content(repository_alias='backend-global', file_path='src/auth.py')
   scip_references(symbol='authenticate_user', repository_alias='backend-global')
   -> Each tool call is automatically recorded as a span within your trace

3. End the trace when your research is complete:
   end_trace(score=0.8, feedback='Found main auth flow, need to check middleware')
   -> Score (0.0-1.0) rates session success, feedback provides context

IMPORTANT FOR HTTP CLIENTS:
If using HTTP-based MCP calls (not stdio), you MUST include session_id for trace persistence:
  POST /mcp?session_id=your-unique-session-id

Without session_id, each HTTP request has isolated state and traces cannot span multiple requests.

AUTO-TRACING:
If the server has auto_trace_enabled configured, a trace is created automatically on your first tool call. However, explicit start_trace is recommended for controlled sessions with meaningful names and metadata.

GRACEFUL BEHAVIOR:
- Tracing is optional - all tools work normally without an active trace
- If Langfuse is disabled/unavailable, tracing tools succeed but do nothing
- Langfuse errors never fail your search or file operations

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

Q: I don't know which repository to search - what do I do?
A: Search cidx-meta-global FIRST! It contains descriptions of all repositories. See Step 4 above.

Q: cidx-meta returned multiple relevant repos - what now?
A: Use multi-repo search (WORKFLOW B). Pass all relevant repos as a list: search_code(query_text='topic', repository_alias=['repo1-global', 'repo2-global'], aggregation_mode='global'). DO NOT search them one-by-one.

Q: What is session tracing and do I need it?
A: Session tracing records your tool calls for observability (performance analysis, debugging, audit trails). It's optional - all tools work without it. Use start_trace() at session start if you want tracking, or enable auto_trace in server config for automatic tracing.

Q: Why did my trace disappear between HTTP requests?
A: HTTP requests have isolated state by default. Include ?session_id=xxx in your MCP endpoint URL to persist trace state across requests. Stdio-based clients don't have this issue.
