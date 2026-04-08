---
name: regex_search
category: search
required_permission: query_repos
tl_dr: Direct pattern search on files without index - comprehensive but slower.
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository identifier(s): String for single repo search, array of strings for omni-regex search across
        multiple repos. Use list_global_repos to see available repositories.'
    pattern:
      type: string
      description: 'Regular expression pattern (ripgrep syntax).'
    path:
      type: string
      description: Subdirectory to search (relative to repo root).
    include_patterns:
      type: array
      items:
        type: string
      description: Glob patterns for files to include.
    exclude_patterns:
      type: array
      items:
        type: string
      description: Glob patterns for files to exclude.
    case_sensitive:
      type: boolean
      description: Case-sensitive matching.
      default: true
    context_lines:
      type: integer
      description: Lines of context before/after match.
      default: 0
      minimum: 0
      maximum: 10
    max_results:
      type: integer
      description: Maximum matches to return.
      default: 100
      minimum: 1
      maximum: 1000
    multiline:
      type: boolean
      description: "Enable multi-line matching. Patterns can span multiple lines using \\n or . (which matches\
        \ newlines with dotall). Uses ripgrep --multiline --multiline-dotall when available, falls back to Python\
        \ re.DOTALL. line_number in results reflects the first line of each match. Example: 'class Foo.*def bar'\
        \ with multiline=true finds class definitions followed by a method on a subsequent line."
      default: false
    pcre2:
      type: boolean
      description: "Enable PCRE2 regex engine for advanced features like lookahead/lookbehind. Requires ripgrep\
        \ built with PCRE2 support (check via rg --pcre2-version). Returns a clear error if PCRE2 is unavailable.\
        \ Example: '(?<=def )\\w+' with pcre2=true finds function names via lookbehind. Combine with multiline=true\
        \ for cross-line lookahead patterns."
      default: false
    response_format:
      type: string
      description: 'Response format for multi-repo queries: flat (default) or grouped by repository'
      enum:
        - flat
        - grouped
      default: flat
    rerank_query:
      type: string
      description: 'Query for cross-encoder reranking. When set, results are reranked by relevance before return. Leave empty for pattern-match order.'
    rerank_instruction:
      type: string
      description: 'Instruction prefix for the reranker (e.g. ''Find implementation, not tests''). Has no effect without rerank_query.'
  required:
  - repository_alias
  - pattern
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether succeeded
    matches:
      type: array
      description: Array of regex match results
      items:
        type: object
        properties:
          file_path:
            type: string
          line_number:
            type: integer
          column:
            type: integer
          line_content:
            type: string
          context_before:
            type: array
            items:
              type: string
          context_after:
            type: array
            items:
              type: string
    total_matches:
      type: integer
    truncated:
      type: boolean
    search_engine:
      type: string
    search_time_ms:
      type: number
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

Exhaustive regex pattern search on repository files without using indexes. Slower than search_code but guarantees finding ALL matches.

KEY DIFFERENCE: regex_search searches files directly (comprehensive, slower) vs search_code FTS mode which uses indexes (fast, approximate). Use regex_search when you need guaranteed complete results.

EXAMPLE: regex_search(repository_alias='backend-global', pattern='def authenticate')

### Reranking Parameters (Optional)

**rerank_query**: When provided, enables cross-encoder reranking to reorder results by semantic relevance.
This is DIFFERENT from the pattern parameter: pattern is a regex used for exact structural matching,
while rerank_query is optimized for cross-encoder scoring (verbose natural language descriptions work better).
Omit rerank_query to return results in pattern-match order (no reranking overhead).

**rerank_instruction**: Optional relevance steering hint passed to the Voyage AI reranker. Has no effect
without rerank_query or when using the Cohere reranker (which receives the instruction concatenated into
the query). Example: "Focus on production authentication code, not test stubs".

#### When to Use Reranking

Regex results have NO semantic ordering — results are ordered by file path or match position, not by
relevance. Cross-encoder reranking adds semantic relevance scoring on top of regex pattern matching,
ensuring the most semantically relevant matches appear first. This is especially valuable when a pattern
matches many files but only a subset are actually relevant to your intent.

#### Examples

**With reranking — finding auth function definitions:**
```json
{
  "pattern": "def.*auth",
  "rerank_query": "authentication and authorization logic that validates user identity or access rights",
  "rerank_instruction": "Focus on production code, not test fixtures or mock helpers",
  "repository_alias": "backend-global",
  "max_results": 20
}
```

**With reranking — finding error handler patterns:**
```json
{
  "pattern": "except.*Exception",
  "rerank_query": "exception handlers that log errors and return meaningful error responses to callers",
  "repository_alias": "backend-global",
  "max_results": 20
}
```

**Without reranking (intentional opt-out):**
```json
{
  "pattern": "def.*auth",
  "repository_alias": "backend-global",
  "max_results": 20
}
```
Result: same matches but in file path / match-position order, with no reranking overhead.
