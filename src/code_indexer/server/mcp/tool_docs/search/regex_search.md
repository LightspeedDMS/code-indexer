---
name: regex_search
category: search
required_permission: query_repos
tl_dr: Direct pattern search on files without index - comprehensive but slower.
slim_description: "Exhaustive regex search on repository files using ripgrep with multiline, PCRE2, context lines, glob filters, and optional reranking."
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
      description: >-
        Glob patterns for files to include. Supports brace groups
        (e.g. "*.{ts,tsx}"), including nested/multiple groups, capped at
        64 expanded variants per pattern. A bare name with no "/" (e.g.
        "docs") matches that name at any depth AND everything under it;
        a single-segment trailing "/" (e.g. "docs/") matches identically
        -- name itself plus contents, at any depth. A MULTI-segment
        trailing "/" (e.g. "src/main/") is root-anchored instead: it
        matches only starting from the repository root, never at any
        depth. See "Glob Pattern Semantics" below.
    exclude_patterns:
      type: array
      items:
        type: string
      description: >-
        Glob patterns for files to exclude. Same brace-group and
        bare-directory semantics as include_patterns.
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
      description: 'Query for cross-encoder reranking. When set, regex hits are semantically reranked before return. Leave empty to preserve the default match order.'
    rerank_instruction:
      type: string
      description: 'Optional instruction prefix for the reranker (e.g. ''Find implementation, not tests''). Has no effect without rerank_query. Steers ranking only; does not change which regex matches are found.'
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
      description: >-
        Number of matches found. Exact only when neither truncated nor
        read_capped is true; otherwise a LOWER BOUND ("at least this many
        matches exist"), not an exact count.
    truncated:
      type: boolean
      description: >-
        True only when the scan affirmatively observed more matches than
        max_results allowed. Distinct from read_capped -- see that field's
        description for how the two differ and can both be true at once.
    read_capped:
      type: boolean
      description: >-
        True when the search hit an internal hard byte-size read ceiling
        before it could scan all output, independent of max_results. When
        the byte ceiling is what stopped the scan before max_results would
        have, truncated is correctly false (the scan cannot know whether
        more than max_results matches existed) and read_capped signals the
        incompleteness instead. Both flags may be true simultaneously when
        the two thresholds are crossed at effectively the same point.
    search_engine:
      type: string
    search_time_ms:
      type: number
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

Exhaustive regex pattern search on repository files without using indexes. Slower than search_code but guarantees finding ALL matches.

KEY DIFFERENCE: regex_search searches files directly (comprehensive, slower) vs search_code FTS mode which uses indexes (fast, approximate). Use regex_search when you need guaranteed complete results.

EXAMPLE: regex_search(repository_alias='backend-global', pattern='def authenticate')

### Glob Pattern Semantics

`include_patterns`/`exclude_patterns` are gitignore-style globs, normalized identically whether the
repository is trigram-indexed or not (both paths produce the same file set for the same pattern):

- A leading `./` is stripped (`./src/*.ts` behaves like `src/*.ts`).
- A "bare token" with no `/`, no glob metacharacter, and no `.` (e.g. `docs`, `Makefile`) is
  ambiguous — it could be a directory name or an extension-less filename — so it matches BOTH the
  name itself at any depth AND everything recursively under it (`**/docs`, `**/docs/**`).
- A pattern with no `/` at all is never anchored to a directory level, even when it carries a glob
  metacharacter or a `.` (unlike the bare-token case above, which requires the ABSENCE of both) —
  `*.py` matches the basename at any depth: `foo.py`, `src/foo.py`, and `src/sub/foo.py` all match.
- A pattern ending in `/` with exactly one path segment (e.g. `docs/`) behaves IDENTICALLY to the
  bare token above — the trailing slash adds no meaning for a single segment: `docs/` also matches
  `docs`, exactly like `docs` does.
- The same any-depth rule applies when that single segment ALSO carries a wildcard (e.g.
  `tests*/`, `build-*/`, `*.d/`) — it is never root-anchored: `tests*/` matches `tests/foo.py`
  (root), `a/tests/foo.py` (nested), and `lib/src/tests_unit/foo.py` (deeply nested, different
  `tests*` variant) alike, not only files directly under a root-level `tests`-prefixed directory.
- A pattern ending in `/` with MORE than one path segment (e.g. `src/main/`) is different: it is an
  explicit, ROOT-ANCHORED directory marker — unlike every case above, it does NOT match at any
  depth. `src/main/` matches `src/main` and everything under it starting from the repository root
  only; `modA/src/main/App.java` (nested under a submodule) is NOT matched. The same rule makes
  `src/*/` match only files inside a NAMED subdirectory of `src/` (`src/sub/x.py`,
  `src/sub/sub2/x.py`), never a file directly in `src/` itself (`src/x.py` does NOT match).
- `*` matches a single path segment; a leading `*/` is rewritten to `**/` (matches at any depth)
  regardless of how many further `/` the rest of the pattern contains, and regardless of whether the
  pattern ends in a wildcard or a bare trailing `/` — `*/tests/*` and `*/tests/` both match
  `tests/foo.py` (zero segments before `tests`), `src/tests/foo.py` (one segment), and
  `a/b/tests/foo.py` (two-or-more segments) alike, not just exactly one segment before `tests`.
  `**` matches multiple path segments recursively.
- Brace groups are supported, including nested and multiple groups in one pattern (e.g.
  `*.{ts,tsx}`, `src/{a,b}/**/*.{js,{jsx,mjs}}`). Each pattern's brace expansion is capped at 64
  variants — a pattern expanding past that cap is rejected as an invalid pattern (see below) rather
  than silently truncated.

### Invalid Pattern Errors

A malformed `include_patterns`/`exclude_patterns` entry is rejected up front, before any search
runs — it is never silently skipped or allowed to widen the search. The response is:

```json
{"success": false, "error": "invalid include_patterns: <reason>"}
```

(or `invalid exclude_patterns: <reason>` for that field). Triggers: a non-string list item (e.g.
`[null]`); an unbalanced brace group (e.g. `*.{ts,md`); gitignore negation/comment syntax used as a
standalone pattern (`!*.md`, `#x`), which has no meaningful effect as an include/exclude pattern
here; or a brace group expanding to more than 64 variants.

**No zero-match-pattern warning**: unlike `xray_search`, `regex_search` does not probe
`include_patterns` for zero-match patterns — an include pattern that matches nothing simply
returns zero matches, with no warning field in the response.

### Reranking Parameters (Optional)

**Mental model — two-query pattern**: Use `pattern` (exact regex) to find matching lines; use `rerank_query` (verbose natural language) to pick the best ordering from those matches. These serve different purposes.

- **rerank_query** = WHAT you want ranked highest. Write a detailed sentence describing your ideal match. The cross-encoder scores each `line_content` against this description.
- **rerank_instruction** = WHAT to deprioritize. Steer the reranker away from noise. Example: "Focus on production authentication code, not test stubs". Has no effect without rerank_query.

#### When to Proactively Add Reranking

Consider adding rerank_query even when the user did not ask for it explicitly:
- The pattern is broad and matches many files, but the user only cares about a subset
- The result set will likely be >5 matches where ordering matters
- File-path or match-position ordering does not reflect what the user actually wants on top

#### When to Use Reranking

Regex results have NO semantic ordering — results are ordered by file path or match position, not by
relevance. Cross-encoder reranking adds semantic relevance scoring on top of regex pattern matching,
ensuring the most semantically relevant matches appear first. This is especially valuable when a pattern
matches many files but only a subset are actually relevant to your intent.

Reranking for regex_search is based on each match's `line_content`, not the full file. It works best when
the matching line carries meaningful context. Very short or ambiguous match lines may rerank poorly.

#### What Reranking Does Not Do

Reranking does NOT find additional regex matches. It only reorders the matches already returned by the
pattern search.

#### When Not to Use Reranking

Skip reranking when doing exhaustive auditing or compliance-style searches where completeness matters but
semantic prioritization does not. It also adds latency for broad searches with many matches.

#### Returned Telemetry

When reranking is requested, the response includes query_metadata with:
- reranker_used
- reranker_provider
- rerank_time_ms

If reranking is requested but providers are disabled, unavailable, or all attempts fail, the tool returns
the base regex ordering and reports that reranking was not used.

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

**With reranking — broad pattern narrowed semantically:**
```json
{
  "pattern": "auth|token|session",
  "rerank_query": "production authentication code that validates tokens or creates authenticated sessions",
  "repository_alias": "backend-global",
  "max_results": 30
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
