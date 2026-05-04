---
name: lightspeed-neo-exploration
description: Guide for exploring and answering questions about LightspeedDMS products using the Neo server. Use this skill when users ask questions about LightspeedDMS products, features, technical implementations, architecture, or any aspect of the codebase. Applies to queries about Evolution DMS, integrations, APIs, database schemas, business logic, or any technical details about LightspeedDMS systems
---

# LightspeedDMS Product Exploration via Neo

This skill provides a structured approach for exploring LightspeedDMS products and answering questions using the CIDX server's code intelligence capabilities.

## Table of Contents

- [Reranking: Use It By Default](#reranking-use-it-by-default)
- [Context Discovery Phase (MANDATORY)](#context-discovery-phase-mandatory--run-before-any-search)
- [Step 1: Start with the Knowledge Base](#step-1-start-with-the-knowledge-base)
- [Step 2: Discover Relevant Repositories via cidx-meta-global](#step-2-discover-relevant-repositories-via-cidx-meta-global)
- [Step 3: Explore Specific Repositories](#step-3-explore-specific-repositories)
- [Step 4: Assess Depth Requirements](#step-4-assess-depth-requirements)
- [Response Guidelines](#response-guidelines)
- [Information Security](#information-security)
- [Advanced Neo Techniques](#advanced-neo-techniques)
- [X-Ray: AST-Aware Precision Search](#x-ray-ast-aware-precision-search)
- [Example Query Patterns](#example-query-patterns)
- [Efficiency Tips](#efficiency-tips)
- [KB Responses](#constructing-knowledge-base-responses)

## Core Principle

When users ask questions about LightspeedDMS products, use Neo to systematically explore the codebase. Answer questions with concepts and natural language rather than disclosing source code by default, unless explicitly asked to reveal code.

## Reranking: Use It By Default

**Every `search_code` call with 2+ word `query_text` in `semantic` or `hybrid` mode should include `rerank_query` and `rerank_instruction`.** This is not optional — it is the standard practice.

**Why:** Skipping reranking on a conceptual query typically costs 2-4 additional follow-up searches to find the right result. Reranking adds ~200-500ms of latency but is almost always cheaper than re-searching.

**Mental model — two-query pattern:**
- `query_text` = short (1-4 keywords) for broad retrieval
- `rerank_query` = verbose natural-language sentence describing your ideal result, for precision ranking
- `rerank_instruction` = what to deprioritize (e.g., image metadata, test files, stack traces)

**Skip reranking only when:** doing an exact single-identifier FTS lookup, result set ≤ 3, or chronological/positional order matters more than relevance.

## Context Discovery Phase (MANDATORY — Run Before Any Search)

**Always complete this phase before performing any `search_code` calls.** Skipping it risks searching the wrong branch and returning results that do not match what is running in the environment being investigated.

### Step 0a: Ask Environment and Repo Context

Before searching any code, ask the user:

> "Are you investigating a production issue, a staging issue, or exploring current development? Which system or repository is involved?"

Wait for the answer. Do not guess. Do not proceed to search until you know:
1. The environment (production / staging / development / other)
2. The system or repository being investigated

### Step 0b: Identify the Target Branch

Once you know the environment and repo, look up the branch-to-environment mapping:

```
search_code(
    query_text="branch_environment_map",
    repository_alias="cidx-meta-global",
    search_mode="fts",
    limit=3
)
```

Also call `get_branches` on the relevant golden repo alias to see what branches are available:

```
get_branches(repo_alias="<repo-name>-global")
```

Match the user's environment to a branch (e.g., production → `release/x.y.z`, staging → `staging`, development → `development`). Present the recommendation to the user and confirm before activating.

### Step 0c: Activate the Correct Workspace

**Never search `*-global` aliases when the user is investigating a specific environment.** Global aliases are pinned to the golden repo's default branch, which is typically `development`. Searching them for a production issue returns wrong-branch results.

Instead, activate a personal workspace on the correct branch:

```
activate_repository(
    golden_repo_alias="<repo-name>",
    branch_name="<target-branch>",
    user_alias="<repo-name>-<env>-workspace"
)
```

This returns a `job_id`. Poll until the workspace is ready:

```
get_job_details(job_id="<job_id>")
```

Repeat every few seconds until `status == "completed"`. Then use `<repo-name>-<env>-workspace` (not `<repo-name>-global`) for all subsequent searches.

### Step 0d: Wrong-Branch Safety Net

Before the first `search_code`, verify the workspace is on the correct branch:

```
get_repository_status(user_alias="<repo-name>-<env>-workspace")
```

Confirm the `current_branch` field matches the target branch. If it does not match, warn the user and offer to re-activate on the correct branch before searching.

**If the user explicitly confirms they want to search the default branch** (e.g., "I just want to explore current development"), it is acceptable to use the `*-global` alias — but state clearly which branch that alias is pinned to.

### Step 0e: Session Lifecycle

- **Check before creating**: Before activating, call `get_repository_status` with the intended alias to see if a workspace already exists from a prior session. If it exists on the correct branch, reuse it.
- **Composite repos**: If the investigation spans multiple repos, use `manage_composite_repository` to create a multi-repo workspace rather than activating each separately.
- **Cleanup**: When the investigation is complete, offer to `deactivate_repository` to free resources. Do not deactivate mid-session.
- **Tags and commits as branch targets**: `branch_name` in `activate_repository` also accepts git tags (e.g., `v10.2.1`) and full commit SHAs for pinning to an exact release.

---

## Discovery Workflow

### Step 1: Start with the Knowledge Base

Always begin exploration with the knowledge-base-global repository, which contains curated product documentation and architectural knowledge.

```
search_code(
    query_text="[user's question keywords]",
    repository_alias="knowledge-base-global",
    search_mode="semantic",
    limit=5,
    rerank_query="[verbose sentence describing the ideal KB article to answer the user's question]",
    rerank_instruction="Focus on product documentation articles, not image metadata, JSON indexes, or Jira examples"
)
```

**Example — user asks "how does BRP warranty integration work":**
```
search_code(
    query_text="BRP warranty integration",
    repository_alias="knowledge-base-global",
    search_mode="semantic",
    limit=5,
    rerank_query="customer-facing documentation explaining how the BRP warranty submission process works in Lightspeed, including setup prerequisites, claim submission from repair orders, and response handling",
    rerank_instruction="Focus on product documentation articles, not image metadata JSON files, stack traces, or Jira how-to guides"
)
```

**Evaluate KB results:**
- If KB provides sufficient information → Answer the question directly
- If KB provides partial information → Note what's covered and identify gaps
- If KB lacks relevant information → Proceed to Step 2

**Critical:**

See section "Constructing Knowledge Base responses" on how to reply back to the user


### Step 2: Discover Relevant Repositories via cidx-meta-global

When knowledge-base-global lacks sufficient information, use cidx-meta-global to identify which repositories contain relevant code. **cidx-meta-global contains two discovery assets — use the right one:**

#### The Dependency Map (`dependency-map/` directory)

The dependency map contains domain-level architectural analysis mapping 36+ domains across 137+ repos. Each domain file documents participating repos, their roles, intra-domain dependencies, and cross-domain connections with concrete evidence (file paths, queue names, API endpoints).

**The dependency map is the single most valuable discovery asset when you don't know where to look.** It is strictly superior to listing repos or guessing, because it gives you domain boundaries, cross-repo data flows, and evidence-backed integration points.

```
search_code(
    query_text="[topic or feature area]",
    repository_alias="cidx-meta-global",
    path_filter="dependency-map/*",
    search_mode="semantic",
    limit=5,
    rerank_query="[verbose description of the domain or integration you're looking for]",
    rerank_instruction="Focus on domain analysis files that describe repo roles and cross-repo data flows"
)
```

**Why dependency-map:**
- "How does lead routing work?" → `dependency-map/prospect-clearinghouse.md` immediately shows all 7 PCH repos, their roles, and the inbound/outbound flow
- "What depends on DMWS?" → `dependency-map/dealer-configuration.md` lists all 16 consumers with code evidence
- "How does OEM data get forwarded?" → `dependency-map/oem-data-forwarding.md` shows the RabbitMQ integration with Evolution

**Read the dependency map result.** It will tell you:
1. Which repos participate in this domain
2. What role each repo plays
3. How they connect (APIs, queues, shared libraries)
4. Where to look next in the actual code

#### Repo Descriptions (root-level `.md` files)

cidx-meta-global also contains AI-generated summaries of what each individual repository contains at the root level.

```
search_code(
    query_text="[topic or feature area]",
    repository_alias="cidx-meta-global",
    search_mode="semantic",
    limit=5,
    rerank_query="[verbose description of the repository capability you're looking for]",
    rerank_instruction="Focus on repository description files that match the feature area"
)
```

The meta-global search returns .md files that describe the repository's contents. Read these descriptions to identify the most relevant repository for the question. Strip `.md` from `file_path` and append `-global` to get the searchable alias (e.g., `auth-service.md` → `auth-service-global`).

#### Decision guide: dependency-map vs repo descriptions

| Signal | Use |
|--------|-----|
| "How does X work?" (cross-cutting) | Dependency map |
| "What calls/depends on X?" | Dependency map |
| "Where does X live in the code?" | Dependency map (for domain context) → then repo search |
| "I have no idea where to start" | **Dependency map** |
| Cross-repo data flow or integration questions | Dependency map |
| "Show me the code in repo Y" (user names the repo) | Skip to Step 3 directly |
| "What does repo Y do?" | Repo description |
| Single-repo internal question | Repo description |

**When in doubt, use the dependency map.** The cost of reading a domain file is a single search call; the cost of guessing wrong is multiple wasted searches across the wrong repos.

### Step 3: Explore Specific Repositories

Once you've identified relevant repositories from meta-global, search those specific codebases:

```
search_code(
    query_text="[specific technical query]",
    repository_alias="[identified-repo-global]",
    search_mode="semantic" or "fts",
    limit=10,
    rerank_query="[verbose sentence describing what you expect the ideal code result to contain]",
    rerank_instruction="[what to deprioritize — e.g., 'Focus on production implementation, not test fixtures or generated code']"
)
```

**Search mode selection:**
- Use `semantic` for conceptual queries ("how does authentication work") — **always add reranking**
- Use `fts` for exact text matching ("class InventoryManager") — reranking optional
- Use `hybrid` when you need both approaches — **always add reranking**

### regex_search vs search_code

Use `regex_search` when:
- You have a known regex pattern (e.g. `def\s+test_`, `class\s+\w+Manager`)
- You need exact match semantics, not semantic similarity
- You are doing a follow-up after `search_code` narrowed the scope and you now need to find specific syntactic patterns within those files

Use `search_code` (semantic mode) when you know a concept but not a syntactic pattern.

For deeper structural distinctions (e.g. "is this method call inside a try-resource block?"), use `xray_search` instead — see the X-Ray section below.

### Step 4: Assess Depth Requirements

Match your investigation depth to the question type:

| Question Type | Tool Strategy | When To Stop |
|---------------|---------------|--------------|
| "What does X do?" (high-level) | One semantic `search_code` + read top 1-2 results | Concept understood, can answer in 2-3 sentences |
| "How does X work?" (technical) | Semantic `search_code` + `regex_search` for exact identifiers + 1-2 file reads | Can describe data flow / control flow |
| "Why does X behave Y way?" (deep technical) | Semantic search + `scip_references` + `git_log` + targeted file reads | Root cause identified with evidence |
| "Find all instances of pattern Y" (precision) | `xray_explore` then `xray_search` | Match count stabilized, no `evaluation_errors` |

If you are 3+ tool calls in and not converging, STOP and ask the user to clarify what level of detail they need.

**High-level questions** → knowledge-base-global + dependency map may be sufficient
- "What features does Evolution DMS have?"
- "How do we handle dealer integrations?"
- "What's our mobile strategy?"

**Technical implementation questions** → Explore specific codebases
- "How is the pricing calculation implemented?"
- "What database tables store inventory data?"
- "How does the API authentication work?"

**Deep technical questions** → Use multiple Neo tools
- Browse directory structure with `browse_directory` or `directory_tree`
- Read specific files with `get_file_content`
- Use SCIP tools for code intelligence (definitions, references, dependencies)
- Search git history with temporal queries if understanding evolution is needed

## Response Guidelines

### Default Response Style

**Concept-based explanations**: Describe how things work using natural language, architectural concepts, and system behavior rather than showing code snippets.

**Example Good Response:**
"The inventory management system uses a layered architecture. The InventoryService class handles business logic for stock tracking, which delegates to the InventoryRepository for database operations. When a vehicle is added, the system validates VIN uniqueness, checks for duplicate entries, and triggers an event for downstream systems to process."

**Example Avoid Unless Requested:**
"Here's the code from InventoryService.cs: [code block]"

### When to Show Code

Only reveal source code when:
1. User explicitly asks to see the code
2. Question specifically requires code examples to understand
3. User needs to reference exact implementation details for development work

Even when showing code, keep snippets focused and contextual rather than dumping entire files.

### Information Security

**Concrete rules for handling sensitive material in search results:**

- **Never quote credentials in your response.** If a search result contains an API key, password, hash, or raw SQL credential, describe its presence/location/format without echoing the value. Example: "There is a hardcoded `password` field at `src/config/db.py:42` containing what appears to be a 16-character base64 string."
- **Redact stack traces with PII.** If a stack trace includes user IDs, email addresses, or request IDs that look real, mask them: `user_id=12345` becomes `user_id=<redacted>`.
- **Refuse to extract content from files matching `*.pem`, `*.key`, `*.env`, `*credentials*`, `*secrets*`.** If a search result hits one of these, report the file existed but do not retrieve content.
- **Be mindful of:** proprietary algorithms or business logic, security-sensitive implementations (authentication, encryption, access control), and trade secrets or competitive advantages.
- **When uncertain, ask.** "I found a result that may contain a credential. Should I describe it generically, or do you need the literal value?"
- **When in doubt,** describe the approach conceptually rather than showing exact code.

## Advanced Neo Techniques

### Multi-Repository Analysis

*Use when a question spans multiple systems and you need to compare or consolidate results across repo boundaries.*

For questions spanning multiple systems, use array syntax:

```
search_code(
    query_text="integration patterns",
    repository_alias=["backend-api-global", "mobile-app-global"],
    aggregation_mode="per_repo",
    limit=10,
    rerank_query="code implementing cross-service integration patterns such as API calls, message queues, or shared data contracts",
    rerank_instruction="Focus on integration glue code, not internal business logic"
)
```

### Temporal Queries

*Use when you need to know what changed between two points in time, or find when a feature was introduced.*

For understanding feature evolution or when code was added:

```
search_code(
    query_text="authentication implementation",
    repository_alias="backend-api-global",
    time_range="2024-01-01..2024-12-31",
    rerank_query="commits or changes that introduced or significantly modified the authentication implementation",
    rerank_instruction="Focus on substantive implementation changes, not formatting or import cleanup"
)
```

Or use git history tools:
- `git_log` for commit history
- `git_search_commits` for finding specific changes
- `git_file_history` for tracking file evolution
- `git_search_diffs` for finding when specific code was added/removed — **use `rerank_query` when the search string is common and matches many commits**

### Code Intelligence (SCIP)

*Use when you know a symbol name and need to navigate its definition, all usages, or trace an execution path with call-graph precision — more reliable than regex for symbol lookup.*

When available, use SCIP tools for precise code navigation:
- `scip_definition` - Find where a class/function is defined
- `scip_references` - Find all usages of a symbol
- `scip_dependencies` - Understand what a component depends on
- `scip_dependents` - Understand what depends on a component
- `scip_impact` - Assess change impact across the codebase
- `scip_callchain` - Trace execution flow between two symbols

**Find all usages of a class:**

```json
scip_references(
    repository_alias="evolution-global",
    symbol="UserManager"
)
```

**Find the definition of a symbol:**

```json
scip_definition(
    repository_alias="evolution-global",
    symbol="processPayment"
)
```

**Trace a call chain:**

```json
scip_callchain(
    repository_alias="evolution-global",
    from_symbol="HttpController.handleRequest",
    to_symbol="DatabaseClient.execute"
)
```

### Repository Structure Exploration

*Use when you need to understand how a repository is organized before diving into code search — orient yourself with the directory layout first.*

For understanding project organization:
- `directory_tree` for visual hierarchy
- `browse_directory` for detailed file listings with metadata
- `list_files` for programmatic file discovery

## X-Ray: AST-Aware Precision Search

Use X-Ray when regex or semantic search produces too many false positives because
the distinction you need is structural, not textual. X-Ray adds an AST filter on
top of a regex driver — only files where the Python evaluator returns True are
included in results.

**When to choose X-Ray vs other tools:**

| Signal | Tool |
|--------|------|
| You know a concept but not a location | `search_code` (semantic) |
| You know an exact identifier or string | `search_code` (fts) or `regex_search` |
| You know a pattern that regex finds, but you need to exclude false positives based on surrounding code structure | **`xray_search`** |
| You want to understand what AST node tree-sitter produces for a construct before writing an evaluator | **`xray_explore`** |
| You need to find all callers/usages of a known symbol with call-graph precision | `scip_references` |

X-Ray is not a replacement for semantic search. It is a precision filter for situations where you already know what the code pattern looks like textually and need to make a structural distinction regex cannot express — e.g., "only calls NOT inside a try-with-resources", "only HTTP clients missing a timeout parameter", "only direct DB writes outside a transaction boundary".

### Two-tool workflow: explore first, then search

Never write a `xray_search` evaluator cold. Always run `xray_explore` first on 2-3
files to see what AST nodes tree-sitter produces for your language and construct.

**Step 1 — Explore (discover AST shape):**

```json
xray_explore(
    repository_alias="evolution-global",
    driver_regex="prepareStatement",
    search_target="content",
    include_patterns=["*.java"],
    max_files=3,
    max_debug_nodes=30
)
```

This returns immediately with a `job_id`. Poll `get_job_details(job_id="<id>")` until
`status == "completed"`. Read the `ast_debug` field in each match — it shows the
breadth-first AST tree rooted at the file, with `type`, `text_preview`, and byte
offsets per node. Find the `type` string for the node you care about (e.g.,
`"method_invocation"`, `"try_with_resources_statement"`).

**Step 2 — Write and test evaluator on 5 files:**

```json
xray_search(
    repository_alias="evolution-global",
    driver_regex="prepareStatement",
    evaluator_code="return node.type == 'method_invocation' and not node.is_descendant_of('try_with_resources_statement')",
    search_target="content",
    include_patterns=["*.java"],
    max_files=5
)
```

Poll `get_job_details` to COMPLETED. Read `evaluation_errors` first. `AttributeError`
entries mean your evaluator referenced a node attribute that does not exist for that
node type — fix the attribute name and re-run. `EvaluatorTimeout` means the expression
is too slow — simplify it. Only proceed to the full run when `evaluation_errors` is
empty on the 5-file test.

**Step 3 — Full search (remove max_files cap):**

Same call as Step 2 without `max_files`. Poll to COMPLETED. If the result has
`truncated: true`, use `get_cached_content(cache_handle="<uuid>", page=1)` to retrieve
pages of results.

### Evaluator API — what you need to know

The evaluator is a single Python expression that must return a bool. It runs in a
sandboxed subprocess. Five names are available:

| Name | What it is |
|------|-----------|
| `node` | Deepest AST node enclosing the regex match position (the "closest ancestor" at the match site). For `search_target="filename"`, this equals `root`. |
| `root` | The file's root AST node — use this to traverse the full file tree. |
| `source` | Full file text as a UTF-8 string. |
| `lang` | Language string: `java`, `kotlin`, `go`, `python`, `typescript`, `javascript`, `bash`, `csharp`, `html`, `css`. |
| `file_path` | Absolute path of the file being evaluated. |

Key methods on `node` and `root`:
- `node.type` — tree-sitter node type string (what you see in `ast_debug`)
- `node.is_descendant_of("node_type_string")` — True if any ancestor in the tree has that type
- `node.named_children` — list of named child nodes
- `node.text` — source text of the node (use `source[node.start_byte:node.end_byte]` as equivalent)

**Safe builtins:** `len`, `str`, `int`, `bool`, `list`, `tuple`, `dict`, `min`, `max`,
`sum`, `any`, `all`, `range`, `enumerate`, `zip`, `sorted`, `reversed`, `hasattr`.

**Not available:** `getattr`, `setattr`, `open`, `eval`, `exec`, `__import__`, and
all dunder attribute access (e.g., `node.__class__` is blocked). Import statements,
loops (`for`, `while`), and `with` blocks are rejected at AST validation time before
any subprocess is spawned.

The hard per-invocation timeout is 5 seconds. Keep evaluators simple — a single
boolean expression on `node.type` and `node.is_descendant_of()` is the typical pattern.

### Async reminder

Both `xray_search` and `xray_explore` return `{job_id}` immediately. The job runs
in the background. You MUST poll:

```
get_job_details(job_id="<returned_job_id>")
```

Repeat until `status == "completed"` or `status == "failed"`. Do not assume the result
is ready after a single poll. Default timeout is 120 seconds; increase `timeout_seconds`
for large repositories (max 600).

### Paged results reminder

X-Ray results from large repositories often exceed the inline preview threshold (~2000
chars). When the polled result has `truncated: true`:

1. Note the `cache_handle` value.
2. Call `get_cached_content(cache_handle="<uuid>", page=1)` for the first page.
3. Increment `page` until the response indicates no further pages.

The `matches_and_errors_preview` field and the first 3 inline `matches[]` entries give
a quick scan without fetching the cache.

### LightspeedDMS-specific X-Ray examples

**Unsafe SQL — prepareStatement outside try-with-resources (Java):**

```json
xray_search(
    repository_alias="evolution-global",
    driver_regex="prepareStatement",
    evaluator_code="return node.type == 'method_invocation' and not node.is_descendant_of('try_with_resources_statement')",
    search_target="content",
    include_patterns=["*.java"],
    exclude_patterns=["*/test/*"]
)
```

Finds every JDBC prepared statement call that is not resource-managed. False positives
from comments or string literals are filtered by the `node.type` check.

**Hardcoded credentials — string literals that look like passwords:**

```json
xray_search(
    repository_alias="evolution-global",
    driver_regex="(?i)(password|passwd|secret|api_key)\\s*=\\s*[\"'][^\"']{6,}[\"']",
    evaluator_code="return node.type in ('string_literal', 'string', 'assignment_expression') and lang in ('java', 'kotlin', 'python')",
    search_target="content",
    exclude_patterns=["*/test/*", "*/resources/i18n/*"]
)
```

**HTTP calls without a timeout (Java/Kotlin):**

```json
xray_search(
    repository_alias="integration-services-global",
    driver_regex="HttpClient|OkHttpClient|RestTemplate",
    evaluator_code="return node.type == 'object_creation_expression' and not any(c.type == 'argument_list' and 'timeout' in source[c.start_byte:c.end_byte].lower() for c in node.named_children)",
    search_target="content",
    include_patterns=["*.java", "*.kt"]
)
```

Note: run `xray_explore` first on 2-3 files to confirm the `object_creation_expression`
node type name for the target language — Kotlin's tree-sitter grammar may use a
different type string.

**Direct DB writes outside a transaction (Java — detecting raw update/insert calls):**

```json
xray_search(
    repository_alias="evolution-global",
    driver_regex="executeUpdate|executeInsert|executeBatch",
    evaluator_code="return node.type == 'method_invocation' and not node.is_descendant_of('try_statement') and not node.is_descendant_of('method_declaration')",
    search_target="content",
    include_patterns=["*.java"],
    exclude_patterns=["*/test/*"]
)
```

This evaluator finds raw `executeUpdate`/`executeInsert`/`executeBatch` calls that are NOT inside a try-block (suggesting no transaction-rollback safety) AND NOT inside a method declaration body (filtering out lambda/closure contexts). For full transaction-detection, follow up with a second X-Ray pass that checks for `@Transactional` annotations or surrounding `try`/`finally` blocks — see "Iterating on Your Evaluator" in the per-tool docs at `tool_docs/search/xray.md`.

## Example Query Patterns

**Product feature question:**
1. Search knowledge-base-global for "dealer portal features" — **with reranking** targeting product documentation
2. If insufficient, search dependency map for the domain — **with reranking** targeting domain analysis files
3. Search identified repos for detailed implementation — **with reranking** targeting production code
4. Synthesize findings into conceptual explanation

**"I have no idea where this lives" question:**
1. Search knowledge-base-global first (may have docs on the topic) — **with reranking**
2. Search `cidx-meta-global` dependency map for the domain/capability — **with reranking**
3. Read the domain file to understand participating repos and their roles
4. Search the identified repos for implementation details — **with reranking**
5. Use SCIP to trace call chains if needed
6. Synthesize findings into conceptual explanation

**Cross-cutting data flow question:**
1. Search knowledge-base-global for overview — **with reranking**
2. Search dependency map for the domain (e.g., prospect-clearinghouse) — **with reranking**
3. Read cross-domain connections to understand the full flow
4. Search each participating repo along the flow path — **with reranking**
5. Trace the data path through code if needed

**Technical implementation question:**
1. Search knowledge-base-global for architectural overview — **with reranking**
2. Search cidx-meta-global (dependency map or repo descriptions) to identify implementation repo — **with reranking**
3. Use `browse_directory` to understand module structure
4. Use `search_code` with FTS mode for specific classes/functions (reranking optional for exact identifiers)
5. Use SCIP tools if needed for code relationships
6. Explain implementation in natural language with architecture context

## Error Recovery

If a search returns no results:
- Broaden search terms
- Try different search modes (semantic vs FTS)
- Search cidx-meta-global without path_filter (both dependency map and repo descriptions)
- Try alternate keywords or synonyms
- Check repository status with `global_repo_status`

If a search returns noisy or irrelevant results:
- **Add or refine `rerank_query`** — write a more specific sentence describing the ideal result
- **Add or refine `rerank_instruction`** — explicitly name the noise you're seeing (e.g., "Deprioritize image metadata JSON, test fixtures, and stack trace examples")
- Use `path_filter` or `exclude_path` to narrow structurally
- Combine reranking with path filters for maximum precision

If you're unsure which repository to search:
- Always consult cidx-meta-global first — **prefer the dependency map** for cross-cutting or unknown-location questions
- Then try repo descriptions in cidx-meta-global for single-repo questions
- Use `list_global_repos` as a last resort to see all available repositories
- Ask the user for clarification if needed

## Efficiency Tips

- Start with smaller `limit` values (3-5) for initial exploration
- Increase limit only if initial results are insufficient
- **Always include `rerank_query` on semantic/hybrid searches** — the ~200-500ms cost is almost always cheaper than the 2-4 extra searches you'll need without it
- Use `path_filter` to narrow searches to relevant directories (e.g., `path_filter="dependency-map/*"` for domain discovery)
- Use `exclude_path` to filter out test files or generated code when not relevant
- Combine reranking with `path_filter`/`exclude_path` for maximum signal-to-noise
- Cache handles are provided for large results - use `get_cached_content` to retrieve full content
- Use `response_format='grouped'` for multi-repo searches when you want results organized by repository
- For `regex_search` with broad patterns returning many matches, add `rerank_query` to push the most relevant matches to the top

**Async tools and large result paging:** Some Neo tools (`activate_repository`,
`xray_search`, `xray_explore`) return a `{job_id}` immediately and run in the
background. Always poll `get_job_details(job_id="<id>")` until `status` reaches
`"completed"` or `"failed"` before reading the result. For any tool result that
returns `truncated: true` and a `cache_handle`, use `get_cached_content(cache_handle,
page=N)` to retrieve the full content page by page.

## Common LightspeedDMS Repositories

Based on context, these repositories are likely available (verify with list_global_repos):
- **knowledge-base-global** - Product knowledge base (always start here for support/product questions)
- **dev-knowledge-base.wiki-global** - Developer knowledge base (architecture, technical guides)
- **cidx-meta-global** - Repository directory AND dependency map (use for code discovery — **dependency map is the best starting point when you don't know where to look**)
- Evolution DMS core systems
- Mobile applications
- Integration services
- API implementations

Always verify current repository names with `list_global_repos` as the structure may evolve.


# Constructing Knowledge Base Responses

## Identifying Knowledge Base Repositories

Any repository in the Neo server with `repo_category: "Documentation"` is a knowledge base / wiki-enabled repository. These repos have a built-in wiki UI served by the Neo server, and their search results include a `wiki_url` field.

Currently known knowledge base repositories (verify with `list_global_repos`):
- **knowledge-base-global** — Main product knowledge base (support articles, troubleshooting guides)
- **dev-knowledge-base.wiki-global** — Developer-focused knowledge base (architecture docs, technical guides)

New knowledge base repos may be added over time. You can identify them by:
1. The `repo_category` field in search results being `"Documentation"`
2. The presence of a `wiki_url` field in search results
3. The `wiki_article_analytics` tool returning `wiki_enabled: true` for the repo

## Constructing Full Wiki URLs

Search results from knowledge base repositories include a `wiki_url` field containing a **relative path** (e.g., `/wiki/knowledge-base/Internal/email-troubleshooting-guide`). To create clickable links, prepend the Neo server base URL.

**Base URL:** `https://codeindexer.lightspeedtools.cloud`

**Formula:** Full URL = `https://codeindexer.lightspeedtools.cloud` + `wiki_url`

Example search result:
```json
{
  "file_path": "Internal/email-troubleshooting-guide.md",
  "repo_category": "Documentation",
  "wiki_url": "/wiki/knowledge-base/Internal/email-troubleshooting-guide"
}
```

Full URL: `https://codeindexer.lightspeedtools.cloud/wiki/knowledge-base/Internal/email-troubleshooting-guide`

## References Section

CRITICAL: When a response draws on knowledge base articles, include a **References** section at the end of the response. This gives users direct links to the source articles for further reading.

Format the References section as follows:

```
---
**References:**
- 📄 [Article Title](https://codeindexer.lightspeedtools.cloud{wiki_url})
- 📄 [Another Article](https://codeindexer.lightspeedtools.cloud{wiki_url})
```

**When to include a References section:**
- The response synthesizes or summarizes information from one or more KB articles
- The response answers a question using KB content
- Multiple articles are relevant and the user may want to explore further

**When NOT to include a References section:**
- No KB articles were found or used
- The response is purely from codebase exploration (non-KB repos) with no Documentation-category results
- A brief inline link is more appropriate (e.g., a single short answer pointing to one article)

## Example Responses

### Example 1: Detailed response with References

User asks: "How do I fix email sending errors?"

Search: `search_code("email sending errors", repository_alias="knowledge-base-global", search_mode="semantic", limit=5, rerank_query="troubleshooting guide for email sending failures in Lightspeed including SMTP configuration and provider-specific fixes", rerank_instruction="Focus on support articles, not image metadata or Jira examples")`

Result includes: `wiki_url: "/wiki/knowledge-base/Internal/email-troubleshooting-guide"`

Response to user:

> The most common cause of email sending errors is an expired or incorrect password on the communication account. Start by remoting into the client's computer and replicating the error, then check that they have security access to System > Lists > Communication Accounts. If the account status is anything other than "Succeeded," it will produce an error.
>
> For specific email providers, the troubleshooting steps vary — Office365 and GoDaddy-hosted accounts often have SMTP authentication disabled by default, and dealers may need to contact GoDaddy support to enable it. Gmail, Yahoo, and AOL accounts typically require app passwords when multi-factor authentication is enabled.
>
> ---
> **References:**
> - 📄 [Email Troubleshooting Guide](https://codeindexer.lightspeedtools.cloud/wiki/knowledge-base/Internal/email-troubleshooting-guide)
> - 📄 [Email Integration Setup Info](https://codeindexer.lightspeedtools.cloud/wiki/knowledge-base/Customer/email-integration-setup-info)

### Example 2: Developer KB article

User asks: "How do DMS client plugins work?"

Search: `search_code("client plugins", repository_alias="dev-knowledge-base.wiki-global", search_mode="semantic", limit=5, rerank_query="architecture documentation explaining how DMS client plugins are loaded, registered, and isolated in the classloader hierarchy", rerank_instruction="Focus on architectural documentation, not changelog entries or code snippets")`

Result includes: `wiki_url: "/wiki/dev-knowledge-base.wiki/DMS-Client-Plugins"`

Response to user:

> The DMS client uses a plugin architecture with two distinct classloader environments. In development, the ConsolidatedRegistrar loads everything through the SystemClassLoader so plugins and application classes can freely reference each other. In production (Windows), the PluginRegistrar uses a separate URLClassLoader for plugin JARs, creating a hard boundary — crossing it causes ClassDefNotFoundError.
>
> Plugin classes must use the LightspeedContext interface to access application-level functions, and the Gradle build in `evolution/code/gradle/client/package-jar.gradle` controls how JARs are bundled.
>
> ---
> **References:**
> - 📄 [DMS Client Plugins](https://codeindexer.lightspeedtools.cloud/wiki/dev-knowledge-base.wiki/DMS-Client-Plugins)

### Example 3: Quick single-article answer (inline link, no References section)

User asks: "Where's the article on rental items?"

> 📄 [Rental Items Setup](https://codeindexer.lightspeedtools.cloud/wiki/knowledge-base/Customer/rental-items)

## Using `wiki_article_analytics`

Use the `wiki_article_analytics` tool to find popular or underviewed articles in a knowledge base repo. This is useful for:
- Finding the most-read articles on a topic
- Identifying articles that may need attention (least viewed)
- Browsing what's available in a knowledge base

```
wiki_article_analytics(
    repo_alias="knowledge-base-global",
    sort_by="most_viewed",
    search_query="email",       # optional: filter by topic
    search_mode="semantic",     # optional: semantic or fts
    limit=10
)
```

Results include `wiki_url`, `real_views`, `first_viewed_at`, and `last_viewed_at` per article. Always construct full URLs using the base URL + `wiki_url` when including links in responses.

## Best Practices

- **Always include full clickable URLs** — Construct them as `https://codeindexer.lightspeedtools.cloud` + the `wiki_url` field from search results
- **Include a References section** for substantial responses that draw on KB articles
- **Search both knowledge bases when relevant** — Use `knowledge-base-global` for support/product questions and `dev-knowledge-base.wiki-global` for developer/architecture questions
- **Provide context** — Include a brief summary of what the article covers alongside the link
- **Multiple results** — If relevant, provide 2-3 links in the References section for comprehensive coverage
- **Check `repo_category`** — Any repo with `repo_category: "Documentation"` in search results is a knowledge base; apply these linking practices to all such repos, not just the ones listed above

## Quick Reference Cheat Sheet

**Tool selection:**
- Concepts → `search_code` (semantic) + reranking
- Exact strings → `search_code` (fts) or `regex_search`
- Structural patterns (AST-level) → `xray_explore` then `xray_search`
- Symbol navigation → `scip_definition`, `scip_references`, `scip_callchain`
- Time-travel → `git_log`, `git_diff`, `git_blame`

**Mandatory pre-search steps:**
1. Ask environment + system (production / staging / development)
2. Verify branch with `get_repository_status`
3. For LightspeedDMS: search `cidx-meta-global` first to find relevant repos

**Rerank by default for 2+ word queries in semantic/hybrid mode.**

**Async tools:** `activate_repository`, `xray_search`, `xray_explore` return `{job_id}` — poll `get_job_details` until COMPLETED.

**Paged retrieval:** when result has `truncated: true`, use `get_cached_content(cache_handle, page=N)`.

**KB responses:** prepend `https://codeindexer.lightspeedtools.cloud` to the `wiki_url` field; always include a References section when drawing on KB articles.
