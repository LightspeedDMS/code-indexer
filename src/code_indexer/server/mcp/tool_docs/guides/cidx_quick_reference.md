---
name: cidx_quick_reference
category: guides
required_permission: query_repos
tl_dr: Get quick reference for CIDX MCP tools with decision guidance.
inputSchema:
  type: object
  properties:
    category:
      type:
      - string
      - 'null'
      enum:
      - search
      - scip
      - git_exploration
      - git_operations
      - files
      - repo_management
      - golden_repos
      - system
      - user_management
      - ssh_keys
      - meta
      - null
      default: null
      description: 'Optional category filter. null/omitted returns all tools. Options: search, scip, git_exploration, git_operations,
        files, repo_management, golden_repos, system, user_management, ssh_keys, meta.'
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether the operation succeeded
    total_tools:
      type: integer
      description: Total number of tools returned
    category_filter:
      type:
      - string
      - 'null'
      description: Category filter applied (null if showing all)
    tools:
      type: array
      description: List of tool summaries
      items:
        type: object
        properties:
          name:
            type: string
            description: Tool name
          category:
            type: string
            description: Tool category
          summary:
            type: string
            description: TL;DR summary from tool description
          required_permission:
            type: string
            description: Permission required to use this tool
        required:
        - name
        - category
        - summary
        - required_permission
    error:
      type: string
      description: Error message if operation failed
  required:
  - success
  - total_tools
  - tools
---

Quick reference card for CIDX tools with decision guidance. Returns dynamic content based on optional category filter.

REPOSITORY DISCOVERY (cidx-meta-global):
cidx-meta-global is a synthetic repository that contains AI-generated markdown descriptions of every other repository registered on this server. Each file in cidx-meta-global is named after a repository (e.g., 'auth-service.md' describes the auth-service-global repository). It also contains a dependency-map/ subdirectory with cross-repository architectural analysis organized by domain.

WHEN TO USE: Before searching for code when you don't know which repository contains the topic. Skip only when the user explicitly names a repository.

DISCOVERY WORKFLOW:
1. search_code(query_text='your topic', repository_alias='cidx-meta-global', limit=5) -- find relevant repos
2. Results will have file_path like 'auth-service.md' or 'dependency-map/authentication.md'
3. For repo description files: strip '.md' from file_path and append '-global' to get the repository alias (e.g., 'auth-service.md' -> 'auth-service-global')
4. For dependency-map files: read the content to find which repos participate in that domain
5. search_code(query_text='your topic', repository_alias='auth-service-global', limit=10) -- search the identified repo

DEPENDENCY MAP (cidx-meta-global/dependency-map/):
When you don't know which repository to search -- or when a topic spans multiple repos -- the dependency map is your best starting point. It contains domain-level architectural analysis that maps which repositories participate in each domain and how they interact (shared APIs, data flows, integration points).

TWO TYPES OF cidx-meta-global RESULTS:
- Repo description files (e.g., 'auth-service.md'): What a SINGLE repository contains. Use when your topic is likely contained in one repo.
- Dependency-map files (e.g., 'dependency-map/authentication.md'): How MULTIPLE repositories collaborate in a domain. Use when your topic crosses repo boundaries, you need architectural context, or you don't know where to start.

DEPENDENCY MAP WORKFLOW:
1. search_code(query_text='your topic', repository_alias='cidx-meta-global', limit=5)
2. If results include dependency-map/ files, read the snippet -- it lists participating repos and their roles
3. Search the identified repos together: search_code(query_text='topic', repository_alias=['repo1-global','repo2-global'], aggregation_mode='per_repo')

IF cidx-meta-global IS NOT AVAILABLE: Fall back to list_global_repos() to see all repos, then search the most likely candidates.

CATEGORIES: search, scip, git_exploration, git_operations, files, repo_management, golden_repos, system, user_management, ssh_keys, meta, tracing

MULTI-REPO: Pass array to repository_alias. aggregation_mode='global' for best matches, 'per_repo' for balanced representation. limit=10 with 3 repos returns 10 TOTAL (not 30).

SEARCH MODE: 'authentication logic' (concept) -> semantic | 'def authenticate_user' (exact) -> fts | unsure -> hybrid

RERANKING: Default is to use reranking for any conceptual or multi-word semantic/hybrid query. Add rerank_query (verbose sentence describing ideal result) and rerank_instruction (what to deprioritize). Skip only for exact identifier lookups, result sets <= 3, or positional ordering. Cost: ~200-500ms. Benefit: saves 2-4 re-searches.

EXAMPLE: cidx_quick_reference(category='search') for search-specific guidance.

#### Shared Technical Memory Store

Neo maintains a persistent memory store at `cidx-meta/memories/*.md`. Memories are short, evidence-backed technical facts indexed alongside code and searchable through the same tools you already use.

**SWEEP MEMORIES BEFORE FORMING OPINIONS.** Before you describe how a subsystem works, explain a config behavior, or claim a particular API contract to the user, run a memory search first. If a memory exists on the topic, trust it over your own inference.

**CHECK MEMORIES on these triggers:**
- The user corrects one of your discoveries, assumptions, or architectural claims
- You encounter an ambiguous concept or suspect a known gotcha
- You are starting investigation on an unfamiliar subsystem
- You are about to answer a "how does X work?" question authoritatively

**How to search memories** (use these exact patterns):

```
# Semantic: concept-based discovery
search_code query_text="known gotcha authentication token expiry" path_filter="cidx-meta/memories/*.md"

# Browse all memories (list)
list_files path="cidx-meta/memories"

# Regex: find memories by type
regex_search pattern="^type: gotcha" path_filter="cidx-meta/memories/*.md"

# Regex: find memories scoped to a specific repo alias
regex_search pattern="scope_target: my-repo-alias" path_filter="cidx-meta/memories/*.md"
```

The `path_filter="cidx-meta/memories/*.md"` is required on every search - without it you will get code results mixed in and miss memories buried below the score threshold.

**Workflow when the user corrects you**:
1. Search memories on the topic (use the patterns above).
2. If a matching memory exists, call `edit_memory` with the new evidence (do not create a duplicate).
3. If no matching memory exists, call `create_memory`.
4. Report the uuid and summary back to the user so they can verify.

Do not treat corrections, architectural explanations, or surfaced gotchas as session-local. If the user told you, it belongs in memory so the next agent session does not repeat the same mistake. **Err on the side of capturing** - sessions end, memories persist.

**Memory types** (choose one when creating):

| Type | Use for |
|------|---------|
| `architectural-fact` | How subsystems relate, data flow, ownership |
| `gotcha` | Non-obvious failures, ordering constraints, traps |
| `config-behavior` | How config keys behave, defaults, interactions |
| `api-contract` | External API params, limits, undocumented behaviors |
| `performance-note` | Measured thresholds, known bottlenecks, optimization results |

**Memories are evidence-backed.** Every memory points to specific `file:lines` or commit hashes. If you cannot provide that evidence, do not write the memory yet - gather it first, then write.

