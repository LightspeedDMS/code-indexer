---
name: xray_search
category: search
required_permission: query_repos
tl_dr: Two-phase AST-aware search -- regex driver narrows candidate files, Rust native evaluator inspects each file's AST and returns a list of findings with pattern name, line number, and code snippet.
slim_description: "Two-phase AST-aware code search: regex pattern narrows files, then Rust native evaluator runs against each file's tree-sitter AST. Evaluator receives the root OwnedNode and returns Vec<EvalFinding>. Supports multi-repo, glob filters, and async polling."
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository identifier(s). String for single-repo search; array of strings (or a JSON-encoded array string, e.g. ''["repo-a","repo-b"]'') for multi-repo search.'
    pattern:
      type: string
      description: 'Regular expression applied in Phase 1 to file content (search_target=content) or relative file paths (search_target=filename) to identify candidate files.'
    evaluator_code:
      type: string
      description: 'Rust code defining fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>. Mutually exclusive with pattern_name. See "Evaluator API" below.'
    search_target:
      type: string
      enum:
        - content
        - filename
      description: 'What pattern applies to: "content" matches file text, "filename" matches relative file paths.'
    include_patterns:
      type: array
      items:
        type: string
      description: >-
        Glob patterns for files to include (e.g. ["*.java", "*.kt"]). Empty
        list means include all. See "Glob Pattern Semantics" below for the
        full matching rules (brace groups, any-depth vs. root-anchored
        segments).
      default: []
    exclude_patterns:
      type: array
      items:
        type: string
      description: >-
        Glob patterns for files to exclude (e.g. ["*/test/*"]). Same
        brace-group and bare-directory semantics as include_patterns.
        Empty list means exclude none.
      default: []
    path:
      type: string
      description: 'Subdirectory within the repository to restrict the Phase 1 content driver to (relative to repo root). null/omitted means the full repository.'
    case_sensitive:
      type: boolean
      description: 'Case-sensitive matching for the Phase 1 content driver regex. Default true.'
      default: true
    context_lines:
      type: integer
      description: 'Lines of context before/after each Phase 1 hit. Range 0..10. Default 0.'
      default: 0
      minimum: 0
      maximum: 10
    multiline:
      type: boolean
      description: 'Enable multi-line regex matching in the Phase 1 content driver. Patterns can span lines via \n or . under dotall. Default false.'
      default: false
    pcre2:
      type: boolean
      description: 'Enable PCRE2 engine for advanced features (lookahead, lookbehind) in the Phase 1 content driver. Requires ripgrep built with PCRE2. Default false.'
      default: false
    timeout_seconds:
      type: integer
      description: 'Per-job wall-clock timeout in seconds. Range 10..600. Default 120. When exceeded, response includes partial=true and timeout=true.'
      minimum: 10
      maximum: 600
      default: 120
    max_results:
      type: integer
      description: 'Maximum number of candidate files to evaluate. Must be >= 1 when provided.'
      minimum: 1
    await_seconds:
      type: number
      description: 'Optional server-side polling window in seconds (floats accepted, e.g. 2.5). Range 0.0..45.0. See intro paragraph above for behavior at 0 vs. > 0.'
      minimum: 0
      maximum: 45.0
      default: 0
    pattern_name:
      type: string
      description: "Name of a stored xray evaluator pattern to use (from the cidx-meta pattern library). Mutually exclusive with evaluator_code. See \"Pattern Library\" below."
    pattern_params:
      type: object
      description: 'JSON object of parameter overrides for the resolved pattern. Only valid when pattern_name is provided. Keys must match parameter names declared in the pattern YAML (UPPER_SNAKE_CASE). Values must be compatible with the declared parameter type (usize, i64, f64, bool, or str). Unknown keys return invalid_parameter error; type-incompatible values return invalid_parameter_type error.'
  required:
    - repository_alias
    - pattern
    - search_target
outputSchema:
  type: object
  properties:
    job_id:
      type: string
      description: 'Single-repo: background job identifier. Poll GET /api/jobs/{job_id} for progress and results.'
    job_ids:
      type: array
      items:
        type: string
      description: 'Multi-repo (array repository_alias): one job id per resolved alias.'
    errors:
      type: array
      items:
        type: object
      description: 'Multi-repo: per-alias resolution failures (e.g. unknown alias). Batch continues for resolvable aliases.'
    matches:
      type: array
      description: 'Inline match list when await_seconds resolves. Each entry has file_path, language, line_number, line_content, pattern, and snippet from the Rust evaluator findings.'
      items:
        type: object
    file_metadata:
      type: array
      description: 'Inline per-file metadata list when await_seconds resolves. One entry per evaluated file. Shape: {file_path}.'
      items:
        type: object
    is_admin:
      type: boolean
      description: 'Job-priority opt-in flag present in GET /api/jobs/{job_id} results. Always false for xray_search and xray_explore jobs -- these handlers never request the admin priority lane. This field does NOT reflect whether the submitting user is an administrator; an admin user submitting xray_search will see is_admin=false.'
    debug_output:
      type: array
      items:
        type: string
      description: 'Debug messages emitted by debug_log() calls in the evaluator. Empty list when no debug_log() calls were made (zero overhead). Present in inline results when await_seconds > 0 resolves; also available in polled job results.'
    error:
      type: string
      description: 'Error code when the request is rejected synchronously.'
    message:
      type: string
      description: 'Human-readable description of the error.'
---

Two-phase AST-aware code search.

PHASE 1 (driver, regex): the `pattern` regex narrows the file set. For `search_target='content'`, RegexSearchService (ripgrep-backed) walks the repo and records every hit's line number, column, line content, and context lines. For `search_target='filename'`, an inline path walker matches relative file paths. Phase 1 honors `path`, `include_patterns`, `exclude_patterns`, `case_sensitive`, `multiline`, `pcre2`, and `context_lines`.

PHASE 2 (evaluator, AST): for each candidate file, tree-sitter parses the file once, then your `evaluator_code` runs as a Rust native evaluator (compiled to a dynamic library). The evaluator receives the file root AST node as an `OwnedNode` and returns `Vec<EvalFinding>` -- a list of findings, each with a pattern name, line number, and code snippet. The server enriches each finding with `file_path` and `language`.

Returns `{job_id}` (single repo) or `{job_ids, errors}` (multi-repo) immediately when `await_seconds` is 0 (default); poll `GET /api/jobs/{job_id}` for results. Set `await_seconds > 0` to have the server poll the background job for up to that many seconds and return the inline result if it completes, falling back to `{job_id}` otherwise (inline-wait capped at 45 seconds, lowered from 120.0 by Bug #1070 -- see the `await_seconds` row in the Parameters table below).

## Quick Start

Minimal working example -- find all function definitions in Python files:

```json
{
  "repository_alias": "my-repo-global",
  "pattern": "def ",
  "search_target": "content",
  "include_patterns": ["*.py"],
  "max_results": 5,
  "await_seconds": 10,
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for f in node.descendants_of_kind(\"function_definition\") {\n        findings.push(EvalFinding {\n            pattern: \"function_def\".to_string(),\n            line: f.start_line,\n            snippet: truncate_snippet(&f.text(), 80),\n        });\n    }\n    findings\n}"
}
```

Key points:
- **Evaluator code is Rust**, not Python. The function signature is always `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`.
- `OwnedNode`, `EvalFinding`, `truncate_snippet`, and `debug_log` are provided automatically -- do not define them.
- Walk the AST downward: `node.descendants_of_kind("node_type")` finds all matching descendants. Use `xray_explore` or `xray_dump_ast` to discover node type names for your language.
- Start with `max_results: 5` to test your evaluator on a small file set before running the full search.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str OR list[str] | yes | -- | Single alias for single-repo search. Array (or JSON-encoded array string) for omni multi-repo search. Multi-repo returns one job id per resolved alias plus an `errors[]` list for unresolved aliases. |
| pattern | str | yes | -- | Regular expression applied in Phase 1. Renamed from `driver_regex` in v10.3.x. |
| evaluator_code | str | no | (default acceptor) | Rust code defining `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`. See "Evaluator API" below. When omitted, the server substitutes a default that produces one finding per Phase 1 hit. |
| search_target | "content" or "filename" | yes | -- | "content" -- Phase 1 regex applies to file text. "filename" -- Phase 1 regex applies to relative paths. |
| include_patterns | list[str] | no | [] | Glob patterns for files to include. Empty means include all. See "Glob Pattern Semantics" below. |
| exclude_patterns | list[str] | no | [] | Glob patterns for files to exclude. Empty means exclude none. Same semantics as include_patterns. |
| path | str | no | null | Subdirectory restriction within the repo (relative). |
| case_sensitive | bool | no | true | Phase 1 content driver case sensitivity. |
| context_lines | int | no | 0 | Lines of context before/after each Phase 1 hit. Range 0..10. |
| multiline | bool | no | false | Multi-line regex matching in the content driver. |
| pcre2 | bool | no | false | PCRE2 engine for the content driver (lookahead/lookbehind). |
| timeout_seconds | int | no | 120 | Per-job wall-clock cap. Range 10..600. |
| max_results | int | no | null | Cap on candidate files evaluated. When hit: `partial=true`, `max_files_reached=true`. Renamed from `max_files` in v10.3.x. |
| await_seconds | float | no | 0 | Server-side inline-wait window (accepts floats, e.g. 2.5). 0 = return job id immediately. Range 0.0..45.0 (lowered from 120.0 by Bug #1070 -- async handlers risk a 504 at the ALB 60s timeout; the server enforces the 45.0 ceiling via `_AWAIT_SECONDS_MAX` in `handlers/xray.py`). Values > 30.0 emit a server warning. Out-of-range or wrong-type values return error code `await_seconds_invalid`. |
| pattern_name | str | no | null | Name of a stored xray evaluator pattern (from the cidx-meta library). Mutually exclusive with `evaluator_code`. When provided, the server loads and resolves the pattern, applying `pattern_params` overrides. Error `mutually_exclusive_params` if both are provided. |
| pattern_params | object | no | null | Parameter overrides for the resolved pattern. Only valid when `pattern_name` is provided. Keys must match declared parameter names (UPPER_SNAKE_CASE); values must be type-compatible. |

> **REST API field names differ**: The REST endpoint `POST /api/xray/search` uses `driver_regex` (not `pattern`) and `max_files` (not `max_results`). The MCP tool uses the renamed fields shown above. When calling the REST API directly, use the original field names.

## Glob Pattern Semantics

`include_patterns`/`exclude_patterns` are gitignore-style globs, normalized identically for both
`search_target` modes and for indexed and unindexed repositories alike:

- A leading `./` is stripped (`./src/*.ts` behaves like `src/*.ts`).
- A "bare token" with no `/`, no glob metacharacter, and no `.` (e.g. `docs`, `Makefile`) is
  ambiguous -- it could be a directory name or an extension-less filename -- so it matches BOTH the
  name itself at any depth AND everything recursively under it (`**/docs`, `**/docs/**`).
- A pattern with no `/` at all is never anchored to a directory level, even when it carries a glob
  metacharacter or a `.` (unlike the bare-token case above, which requires the ABSENCE of both) --
  `*.java` matches the basename at any depth: `Foo.java`, `src/Foo.java`, and `src/sub/Foo.java` all
  match.
- A pattern ending in `/` with exactly one path segment (e.g. `docs/`) behaves IDENTICALLY to the
  bare token above -- the trailing slash adds no meaning for a single segment.
- The same any-depth rule applies when that single segment ALSO carries a wildcard (e.g.
  `tests*/`, `build-*/`, `*.d/`) -- it is never root-anchored: `tests*/` matches `tests/foo.py`
  (root), `a/tests/foo.py` (nested), and `lib/src/tests_unit/foo.py` (deeply nested, different
  `tests*` variant) alike, not only files directly under a root-level `tests`-prefixed directory.
- A pattern ending in `/` with MORE than one path segment (e.g. `src/main/`) is different: it is an
  explicit, ROOT-ANCHORED directory marker -- unlike every case above, it does NOT match at any
  depth. `src/main/` matches `src/main` and everything under it starting from the repository root
  only; `modA/src/main/App.java` (nested under a submodule) is NOT matched. The same rule makes
  `src/*/` match only files inside a NAMED subdirectory of `src/` (`src/sub/x.java`,
  `src/sub/sub2/x.java`), never a file directly in `src/` itself (`src/x.java` does NOT match).
- `*` matches a single path segment; a leading `*/` is rewritten to `**/` (matches at any depth)
  regardless of how many further `/` the rest of the pattern contains, and regardless of whether the
  pattern ends in a wildcard or a bare trailing `/` -- `*/tests/*` and `*/tests/` both match
  `tests/foo.py` (zero segments before `tests`), `src/tests/foo.py` (one segment), and
  `a/b/tests/foo.py` (two-or-more segments) alike. `**` matches multiple path segments recursively.
- Brace groups are supported, including nested and multiple groups in one pattern (e.g.
  `*.{ts,tsx}`), capped at 64 expanded variants per pattern -- exceeding the cap is an invalid
  pattern (see below), not a silent truncation.

## Invalid Pattern Errors

A malformed `include_patterns`/`exclude_patterns` entry is rejected up front, before evaluator
validation or any background job is submitted -- it is never silently skipped or allowed to widen
the search. The response is returned immediately (synchronously, no `job_id`):

```json
{"error": "include_patterns_invalid", "message": "<reason>"}
```

(or `exclude_patterns_invalid` for that field). Triggers: the field is present but not a list
(including falsy-but-not-a-list values like `""` or `0`); a non-string list item (e.g. `[null]`);
an unbalanced brace group (e.g. `*.{ts,md`); gitignore negation/comment syntax used as a standalone
pattern (`!*.md`, `#x`); or a brace group expanding to more than 64 variants.

## Evaluator API

### File-as-unit contract

The evaluator runs **ONCE per candidate file**, not per Phase 1 hit. It receives the file's root AST node as an `OwnedNode` and returns zero or more `EvalFinding` values describing what it found.

```rust
// Minimum viable evaluator -- accepts every file, produces no findings
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
```

```rust
// Report every function definition in the file
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for f in node.descendants_of_kind("function_definition") {
        findings.push(EvalFinding {
            pattern: "function_def".to_string(),
            line: f.start_line,
            snippet: f.text().chars().take(80).collect(),
        });
    }
    findings
}
```

### Function signature

```rust
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>
```

The evaluator receives the file's root AST node. The `OwnedNode` and `EvalFinding` types are injected automatically by the compiler preamble -- do not define them in your evaluator code.

- **Input**: `node: &OwnedNode` -- the root node of the file's tree-sitter parse tree. Access the full source text via `node.text()`. Walk descendant nodes via `node.descendants_of_kind(...)`.
- **Output**: `Vec<EvalFinding>` -- a list of zero or more findings. An empty Vec means the file matched Phase 1 but the evaluator found nothing noteworthy.

### EvalFinding struct

Each finding describes one result from the evaluator:

```rust
pub struct EvalFinding {
    pub pattern: String,  // pattern name identifying the finding (e.g. "sql_injection", "missing_docstring")
    pub line: usize,      // 1-based line number where the finding occurs
    pub snippet: String,  // code snippet providing context (typically first 80-120 chars of the node text)
}
```

- `pattern`: a short, descriptive label for the kind of finding. This surfaces in the response `matches[]` as the `pattern` field. Use consistent names across findings to allow grouping/filtering.
- `line`: 1-based line number. Use `node.start_line` directly -- it is already 1-based.
- `snippet`: code context for the finding. Typically the node's text truncated to a reasonable length: `node.text().chars().take(80).collect()`.

### debug_log() function

Use `debug_log(msg: &str)` inside your evaluator to trace execution and diagnose unexpected results. Debug messages are collected in-memory and returned in the `debug_output[]` field of the result.

```rust
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    debug_log("evaluator called");
    debug_log(&format!("root kind: {}", node.kind));
    for f in node.descendants_of_kind("function_definition") {
        debug_log(&format!("found function at line {}", f.start_line));
    }
    vec![]
}
```

**Limits** (enforced at runtime, not compile time):
- Maximum 100 messages per evaluation run.
- Maximum 10 KB total across all messages.
- Messages past either limit are silently dropped.

**Security**: `debug_log` writes to an in-memory buffer only. It does NOT perform any I/O, network access, or file system access. It is fully sandboxed.

When no `debug_log()` calls are made, `debug_output` is an empty list (zero overhead).

### OwnedNode reference

The full public surface of any `OwnedNode` reachable via `node`, `descendants_of_kind`, `children`, or `named_children()`:

| Name | Type | Description |
|------|------|-------------|
| `node.kind` | String | tree-sitter node type, e.g. `"function_definition"`, `"call"`, `"if_statement"` |
| `node.children` | Vec\<OwnedNode\> | all child nodes including anonymous ones (punctuation, keywords) |
| `node.named_children()` | Vec\<&OwnedNode\> | only named children -- usually what you want |
| `node.start_byte` | usize | byte offset where the node starts |
| `node.end_byte` | usize | byte offset where the node ends |
| `node.start_line` | usize | 1-based line number where the node starts |
| `node.is_named` | bool | true for named nodes (not anonymous punctuation/keywords) |
| `node.text()` | &str | raw source text of this node |
| `node.child_by_kind(kind)` | Option\<&OwnedNode\> | first child whose `kind` matches the given string |
| `node.has_descendant_of_kind(kind)` | bool | true if any descendant matches `kind` -- use for fast existence checks without allocating |
| `node.descendants_of_kind(kind)` | Vec\<&OwnedNode\> | DFS pre-order; all descendants whose kind matches (excludes self) |

**Not available in OwnedNode** (differs from the former Python XRayNode API):

- No `parent` field -- OwnedNode is a tree of owned children, no parent pointers
- No `enclosing(type_name)` -- cannot walk UP the tree; structure your evaluator to walk DOWN from the root
- No `child_by_field_name(name)` -- use `child_by_kind(kind)` or iterate `named_children()` manually
- No `is_descendant_of(type_name)` -- check containment by walking DOWN from ancestors instead
- No `count_descendants_of_type(name)` -- use `node.descendants_of_kind(name).len()`
- No `start_point` / `end_point` tuples -- use `start_line` (already 1-based) and `start_byte` / `end_byte`

### Rust security whitelist

The server validates evaluator code against a security whitelist before compilation. Forbidden constructs are rejected at validation time with a structured error response.

**Forbidden constructs** (rejected at validation time):

- `unsafe` blocks and `unsafe fn` declarations
- Standard library I/O imports: `std::fs`, `std::net`, `std::process`, `std::env`, `std::io`
- Raw pointers: `*const`, `*mut`
- Foreign function interface: `extern` blocks, `extern "C" fn`
- Module declarations: `mod`
- Static declarations: `static` and `static mut` (use `const` instead)
- Forbidden macros: `include!`, `include_str!`, `include_bytes!`, `env!`, `option_env!`, `println!`, `eprintln!`, `print!`, `eprint!`, `panic!`, `todo!`, `unimplemented!`

**Allowed constructs** (safe Rust subset):

- Variable bindings: `let`, `let mut`, `const`
- Control flow: `if`/`else`, `for`/`while`/`loop`, `match`, `break`, `continue`, `return`
- Local function definitions: `fn` (helper functions within the evaluator)
- String operations: `.to_string()`, `.clone()`, `.contains()`, `.starts_with()`, `.ends_with()`, `.to_uppercase()`, `.to_lowercase()`, `.split()`, `.trim()`, `format!`
- Vec operations: `Vec::new()`, `vec![]`, `.push()`, `.len()`, `.iter()`, `.is_empty()`
- Standard library collections: `std::collections::HashMap`, `HashSet`, `BTreeMap`, `BTreeSet` (import with `use`)
- Iterator chains: `.filter()`, `.map()`, `.any()`, `.all()`, `.count()`, `.flat_map()`, `.enumerate()`, `.collect()`
- Pattern matching, closures, `Option`/`Result` combinators (`.unwrap_or()`, `.map()`, `.and_then()`)
- Safe macros: `format!`, `vec![]`, `assert!`, `assert_eq!`, `assert_ne!`
- The `OwnedNode` and `EvalFinding` types (provided by compiler preamble)
- `debug_log(msg: &str)` -- trace evaluator execution; messages returned in `debug_output[]` (provided by compiler preamble, see "debug_log() function" section)
- `truncate_snippet(s: &str, max_len: usize) -> String` -- collapse whitespace and truncate to `max_len` bytes on a UTF-8 boundary, appending "..." if truncated (provided by compiler preamble)

> **Termination guarantee**: infinite loops in your evaluator hit the sandbox hard timeout (HARD_TIMEOUT_SECONDS = 5.0 s) and surface as `EvaluatorTimeout` in `evaluation_errors[]`. The sandbox timeout is the authoritative termination boundary.

### Cookbook: 17 worked patterns

Each example is a complete `evaluator_code` value. All patterns return `Vec<EvalFinding>`. Structure evaluators by walking DOWN from the root node using `descendants_of_kind`.

1. **Find all function definitions in each file**:
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       for f in node.descendants_of_kind("function_definition") {
           findings.push(EvalFinding {
               pattern: "function_def".to_string(),
               line: f.start_line,
               snippet: f.text().chars().take(80).collect(),
           });
       }
       findings
   }
   ```

2. **Find calls that are NOT inside comments or string literals** (whole-file scan, filters out noise):
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       let comments = node.descendants_of_kind("comment");
       let strings = node.descendants_of_kind("string");
       for call in node.descendants_of_kind("call") {
           let in_excluded = comments.iter().any(|c| c.start_byte <= call.start_byte && call.end_byte <= c.end_byte)
               || strings.iter().any(|s| s.start_byte <= call.start_byte && call.end_byte <= s.end_byte);
           if !in_excluded {
               findings.push(EvalFinding {
                   pattern: "call_outside_comment_string".to_string(),
                   line: call.start_line,
                   snippet: call.text().chars().take(80).collect(),
               });
           }
       }
       findings
   }
   ```

3. **Find calls to a specific function name**:
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       let target_name = "public_method";
       for call in node.descendants_of_kind("call") {
           if call.text().contains(target_name) {
               findings.push(EvalFinding {
                   pattern: "target_call".to_string(),
                   line: call.start_line,
                   snippet: call.text().chars().take(80).collect(),
               });
           }
       }
       findings
   }
   ```

4. **Find functions with N+ elif clauses** (whole-file complexity scan):
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       for f in node.descendants_of_kind("function_definition") {
           let elif_count = f.descendants_of_kind("elif_clause").len();
           if elif_count >= 5 {
               findings.push(EvalFinding {
                   pattern: "high_elif_count".to_string(),
                   line: f.start_line,
                   snippet: format!("elif_count={}", elif_count),
               });
           }
       }
       findings
   }
   ```

5. **SQL-shaped string literals** (find strings containing SELECT):
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       for s in node.descendants_of_kind("string") {
           let text = s.text().to_string();
           if text.contains("SELECT") || text.contains("select") {
               findings.push(EvalFinding {
                   pattern: "sql_string".to_string(),
                   line: s.start_line,
                   snippet: s.text().chars().take(80).collect(),
               });
           }
       }
       findings
   }
   ```

6. **Functions with too many parameters** (whole-file metric):
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       for f in node.descendants_of_kind("function_definition") {
           if let Some(params) = f.child_by_kind("parameters") {
               let param_count = params.named_children().len();
               if param_count > 7 {
                   findings.push(EvalFinding {
                       pattern: "too_many_params".to_string(),
                       line: f.start_line,
                       snippet: format!("param_count={}", param_count),
                   });
               }
           }
       }
       findings
   }
   ```

7. **Deep nesting detection** (whole-file branching score):
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       let ifs = node.descendants_of_kind("if_statement").len();
       let fors = node.descendants_of_kind("for_statement").len();
       let whiles = node.descendants_of_kind("while_statement").len();
       let total = ifs + fors + whiles;
       if total >= 10 {
           findings.push(EvalFinding {
               pattern: "deep_nesting".to_string(),
               line: 1,
               snippet: format!("ifs={} fors={} whiles={} total={}", ifs, fors, whiles, total),
           });
       }
       findings
   }
   ```

8. **List comprehension presence anywhere in the file** (Python-specific):
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       let comps = node.descendants_of_kind("list_comprehension");
       if !comps.is_empty() {
           findings.push(EvalFinding {
               pattern: "list_comprehension".to_string(),
               line: comps[0].start_line,
               snippet: format!("count={}", comps.len()),
           });
       }
       findings
   }
   ```

9. **Functions with high cyclomatic complexity** (per-function score):
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       for f in node.descendants_of_kind("function_definition") {
           let complexity = f.descendants_of_kind("if_statement").len()
               + f.descendants_of_kind("elif_clause").len();
           if complexity >= 8 {
               findings.push(EvalFinding {
                   pattern: "high_complexity".to_string(),
                   line: f.start_line,
                   snippet: format!("complexity={}", complexity),
               });
           }
       }
       findings
   }
   ```

10. **Return statements inside if statements** (walk down from each function):
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for if_stmt in node.descendants_of_kind("if_statement") {
            for ret in if_stmt.descendants_of_kind("return_statement") {
                findings.push(EvalFinding {
                    pattern: "return_inside_if".to_string(),
                    line: ret.start_line,
                    snippet: ret.text().chars().take(80).collect(),
                });
            }
        }
        findings
    }
    ```

11. **Calls without try/except guards** (find calls not nested in try blocks):
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        let try_blocks = node.descendants_of_kind("try_statement");
        for call in node.descendants_of_kind("call") {
            let in_try = try_blocks.iter().any(|t| {
                t.start_byte <= call.start_byte && call.end_byte <= t.end_byte
            });
            if !in_try {
                findings.push(EvalFinding {
                    pattern: "unguarded_call".to_string(),
                    line: call.start_line,
                    snippet: call.text().chars().take(80).collect(),
                });
            }
        }
        findings
    }
    ```

12. **TODO/FIXME comments** (one finding per matching comment):
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for c in node.descendants_of_kind("comment") {
            if c.text().contains("TODO") || c.text().contains("FIXME") {
                findings.push(EvalFinding {
                    pattern: "todo_fixme".to_string(),
                    line: c.start_line,
                    snippet: c.text().chars().take(120).collect(),
                });
            }
        }
        findings
    }
    ```

13. **Public functions missing return-type annotations (Python)**:
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for f in node.descendants_of_kind("function_definition") {
            let children = f.named_children();
            if children.is_empty() {
                continue;
            }
            let name_text = children[0].text();
            if name_text.starts_with("_") {
                continue;
            }
            let has_return_type = children.iter().any(|c| c.kind == "type");
            if !has_return_type {
                findings.push(EvalFinding {
                    pattern: "missing_return_type".to_string(),
                    line: f.start_line,
                    snippet: format!("function: {}", name_text),
                });
            }
        }
        findings
    }
    ```

14. **Bare `except:` clauses** (one finding per bare except):
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for e in node.descendants_of_kind("except_clause") {
            let is_typed = e.named_children().iter().any(|c| {
                c.kind == "identifier" || c.kind == "attribute" || c.kind == "tuple"
            });
            if !is_typed {
                findings.push(EvalFinding {
                    pattern: "bare_except".to_string(),
                    line: e.start_line,
                    snippet: e.text().chars().take(80).collect(),
                });
            }
        }
        findings
    }
    ```

15. **Classes with no docstring** (one finding per undocumented class):
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for cls in node.descendants_of_kind("class_definition") {
            let children = cls.named_children();
            let class_name = if !children.is_empty() {
                children[0].text().to_string()
            } else {
                "<unnamed>".to_string()
            };
            let has_doc = children.iter().any(|c| {
                if c.kind == "block" {
                    let block_children = c.named_children();
                    if !block_children.is_empty() && block_children[0].kind == "expression_statement" {
                        return block_children[0].has_descendant_of_kind("string");
                    }
                }
                false
            });
            if !has_doc {
                findings.push(EvalFinding {
                    pattern: "class_no_docstring".to_string(),
                    line: cls.start_line,
                    snippet: format!("class: {}", class_name),
                });
            }
        }
        findings
    }
    ```

16. **C functions with deeply-nested control flow** (per-function branch density -- C uses `function_definition`, `if_statement`, `for_statement`, `while_statement`):
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for f in node.descendants_of_kind("function_definition") {
            let ifs = f.descendants_of_kind("if_statement").len();
            let fors = f.descendants_of_kind("for_statement").len();
            let whiles = f.descendants_of_kind("while_statement").len();
            let total = ifs + fors + whiles;
            if total >= 8 {
                findings.push(EvalFinding {
                    pattern: "c_deep_control_flow".to_string(),
                    line: f.start_line,
                    snippet: format!("ifs={} fors={} whiles={} total={}", ifs, fors, whiles, total),
                });
            }
        }
        findings
    }
    ```

17. **C++ empty `catch` blocks** (swallowed exceptions -- C++ uses `try_statement` with `catch_clause`; an empty handler contains no `call_expression`, `if_statement`, `for_statement`, `while_statement`, or nested `try_statement`):
    ```rust
    fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
        let mut findings = Vec::new();
        for try_stmt in node.descendants_of_kind("try_statement") {
            for catch in try_stmt.descendants_of_kind("catch_clause") {
                let has_body = catch.has_descendant_of_kind("call_expression")
                    || catch.has_descendant_of_kind("if_statement")
                    || catch.has_descendant_of_kind("for_statement")
                    || catch.has_descendant_of_kind("while_statement")
                    || catch.has_descendant_of_kind("try_statement");
                if !has_body {
                    findings.push(EvalFinding {
                        pattern: "cpp_empty_catch".to_string(),
                        line: catch.start_line,
                        snippet: catch.text().chars().take(80).collect(),
                    });
                }
            }
        }
        findings
    }
    ```

### Common cross-language node type names

tree-sitter node type names differ between languages. Use this table as a starting reference for the general-purpose languages. Bash, HTML, CSS, HCL/Terraform, YAML, SQL, XML, and Groovy are also supported but have limited overlap with the constructs below -- use `xray_dump_ast` to discover their node types.

| Construct | Python | Java | TypeScript / JavaScript | Go | Kotlin | C# |
|-----------|--------|------|-------------------------|-----|--------|-----|
| Function definition | `function_definition` | `method_declaration` | `function_declaration` / `method_definition` | `function_declaration` | `function_declaration` | `method_declaration` |
| Function call | `call` | `method_invocation` | `call_expression` | `call_expression` | `call_expression` | `invocation_expression` |
| Class definition | `class_definition` | `class_declaration` | `class_declaration` | (no class -- `type_declaration` for structs) | `class_declaration` | `class_declaration` |
| If statement | `if_statement` | `if_statement` | `if_statement` | `if_statement` | `if_expression` | `if_statement` |
| Else-if | `elif_clause` (Python-only) | `else if` chain inside `if_statement` | `else if` chain | `else if` chain | `else if` chain | `else if` chain |
| For loop | `for_statement` | `enhanced_for_statement` / `for_statement` | `for_statement` / `for_in_statement` | `for_statement` | `for_statement` | `for_statement` |
| Try block | `try_statement` | `try_statement` / `try_with_resources_statement` | `try_statement` | (no try -- `defer`/`recover`) | `try_expression` | `try_statement` |
| Variable declaration | `assignment` (no separate decl) | `local_variable_declaration` | `lexical_declaration` (`let`/`const`) / `variable_declaration` (`var`) | `var_declaration` / `short_var_declaration` | `property_declaration` | `local_declaration_statement` |
| String literal | `string` | `string_literal` | `string` | `interpreted_string_literal` | `string_literal` | `string_literal` |
| Comment | `comment` | `line_comment` / `block_comment` | `comment` | `comment` | `line_comment` / `block_comment` | `comment` |

C and C++ (verified against tree-sitter-c 0.24.2 and tree-sitter-cpp 0.23.4) share most node kinds. The root node is `translation_unit` in both.

| Construct | C | C++ |
|-----------|---|-----|
| Function definition | `function_definition` | `function_definition` |
| Function call | `call_expression` | `call_expression` |
| Struct / class | `struct_specifier` | `class_specifier` (also `struct_specifier`) |
| Namespace | (none) | `namespace_definition` |
| Template | (none) | `template_declaration` |
| If statement | `if_statement` | `if_statement` |
| For loop | `for_statement` | `for_statement` |
| While loop | `while_statement` | `while_statement` |
| Try / catch | (none -- C has no exceptions) | `try_statement` / `catch_clause` |
| String literal | `string_literal` | `string_literal` |
| Comment | `comment` | `comment` |

For other C/C++ constructs not listed above (variable declarations, preprocessor directives, etc.), use `xray_dump_ast`.

The fastest way to discover the exact type names for a construct is to use `xray_dump_ast` on a small example file in the language, or consult the tree-sitter grammar repository for that language.

## Polled Result Shape

After polling `GET /api/jobs/{job_id}` to COMPLETED status, `result` contains:

- `matches[]`: list of enriched match dicts. Every entry contains:
  - `file_path` (str, server-added) -- relative path from the repository root
  - `language` (str, server-added) -- tree-sitter language name
  - `line_number` (int, from EvalFinding.line) -- 1-based line number
  - `line_content` (str, server-derived) -- raw text of `line_number` from the file source
  - `pattern` (str, from EvalFinding.pattern) -- the pattern name identifying the finding
  - `snippet` (str, from EvalFinding.snippet) -- code snippet context from the evaluator
- `file_metadata[]`: list of per-file metadata entries. One entry per evaluated file. Shape: `{"file_path": str}`.
- `evaluation_errors[]`: list of per-file failures. Each entry: `{file_path, line_number, error_type, error_message}`. `evaluation_errors` does NOT cause job failure -- status remains COMPLETED.
- `files_processed` (int): number of candidate files evaluated.
- `files_total` (int): total candidate files found by Phase 1.
- `elapsed_seconds` (float)
- `partial: true` (only on partial completion)
- `timeout: true` (only when job-level timeout fired -- takes precedence over `max_files_reached`)
- `max_files_reached: true` (only when the `max_results` cap fired before timeout)
- `warnings[]` (only when present): advisory diagnostics from the Phase 1 zero-match-pattern probe.
  Never causes job failure and never changes `matches[]`. Possible `type` values:
  - `zero_match_include_pattern`: this `include_pattern` matched zero files. Carries `pattern` and a
    `hint` suggesting `**/name` for recursive matching. A likely sign the pattern is narrower than
    intended.
  - `zero_match_probe_incomplete` (`search_target="filename"` only): the probe's bounded comparison
    budget (10,000 pattern-to-file comparisons, shared across ALL `include_patterns` in this
    request) was exhausted before every file could be checked against every pattern. Carries the
    `patterns` that could not be fully checked. Meaning for the caller: some genuine zero-match
    patterns among those listed may have gone unreported -- absence of a
    `zero_match_include_pattern` warning for one of them is NOT proof it matched something. The
    main search's `matches[]` are unaffected; this only limits the diagnostic's completeness.
  - `zero_match_probe_timeout` (`search_target="content"` only): the ripgrep-backed probe ran out of
    its shared time budget before checking every `include_pattern`. Same caller implication as
    `zero_match_probe_incomplete` -- some zero-match warnings may be missing; results are unaffected.
  - `content_search_read_capped` (`search_target="content"` only): a probe's own broadest-possible
    scan hit the internal byte-size read ceiling, so that pattern's zero-match determination may be
    unreliable.

### evaluation_errors[] payload examples

Each `error_type` carries a distinct `error_message` shape:

**EvaluatorTimeout** -- sandbox 5s wall-clock budget exceeded:

```json
{
  "file_path": "src/code_indexer/server/services/very_large_module.py",
  "line_number": 0,
  "error_type": "EvaluatorTimeout",
  "error_message": "evaluator exceeded 5s sandbox limit"
}
```

**EvaluatorCrash** -- evaluator process died before returning a value. The `error_message` carries the failure detail:

```json
{
  "file_path": "src/code_indexer/cli.py",
  "line_number": 0,
  "error_type": "EvaluatorCrash",
  "error_message": "evaluator process exited with non-zero status"
}
```

**UnsupportedLanguage** -- Phase 1 selected a candidate whose extension has no tree-sitter grammar:

```json
{
  "file_path": "docs/architecture.md",
  "line_number": 0,
  "error_type": "UnsupportedLanguage",
  "error_message": "No grammar for extension '.md'"
}
```

**ValidationFailed** -- appears in `evaluation_errors[]` only as a defensive fallback. The handler validates `evaluator_code` synchronously BEFORE submitting the job and returns a sync error response with structured fields:

```json
{
  "error": "xray_evaluator_validation_failed",
  "error_code": "forbidden_unsafe",
  "offending_construct": "unsafe",
  "offending_line": 3,
  "message": "forbidden construct 'unsafe' is not allowed in Rust evaluator code"
}
```

Structured error fields: `error_code` identifies the category (e.g. `forbidden_unsafe`, `forbidden_import`, `forbidden_raw_pointer`, `forbidden_extern`, `forbidden_mod`, `forbidden_static`, `forbidden_macro`), `offending_construct` names the specific construct (e.g. `unsafe`, `std::fs`, `*const`, `extern`, `mod`, `static`, `include!`), `offending_line` is the 1-based line number in evaluator_code.

Other validation rejection messages name the offending construct and include a description. Examples: `"forbidden construct 'unsafe' is not allowed in Rust evaluator code"`, `"forbidden import 'std::fs' is not allowed in Rust evaluator code"`, `"forbidden raw pointer '*const' is not allowed in Rust evaluator code"`, `"forbidden macro 'println!' is not allowed in Rust evaluator code"`.

**Generic exception types** (e.g. `IOError`, `UnicodeDecodeError`) -- emitted by the catch-all when the file cannot be read or parsed. The `error_type` is the exception class name; `error_message` is the error detail string.

### Large Result Paging

For results larger than the server's single payload character budget (Web UI `payload_max_fetch_size_chars`, default 5000 chars), the polled job result is truncated and the FULL set is stored in PayloadCache as a series of whole-entry pages. The inline budget IS the page budget -- there is no separate, smaller preview threshold. `matches[]`/`evaluation_errors[]` come back inline as many WHOLE leading entries as fit within `payload_max_fetch_size_chars` -- a result with many small entries can inline dozens of them. **No entry is ever partially cut to fit a page**: an entry that alone exceeds the budget gets its own, necessarily oversized, page in the cache, intact and unmodified. Additional fields:

- `truncated: true` -- set when the matches+errors JSON exceeded the character budget
- `has_more: true` -- synonym; set with `truncated`
- `cache_handle: "<uuid>"` -- opaque handle for paged retrieval
- `total_pages: <int>` -- number of independently-fetchable cache pages the full content was split across
- `inline_entry_truncated: true` -- present only in the rare case where the single first entry alone exceeds the budget; it is then adaptively shrunk (string/list fields capped) for THIS inline response ONLY -- the cached copy stays whole and unmodified
- `matches[]` and `evaluation_errors[]` -- as many whole leading entries as fit the character budget (never a fixed count, never a partially-truncated entry unless `inline_entry_truncated` is set)
- `fetch_tool_hint` -- names `cidx_fetch_cached_payload` and describes the page format below

To fetch the full content, use the discoverable `cidx_fetch_cached_payload` MCP tool with `cache_handle`, incrementing `page` from 1 through `total_pages` (or until `has_more` is `false`). Each page is stored as its own independent cache row and is returned WHOLE and unsliced regardless of the server's CURRENT `payload_max_fetch_size_chars` setting -- a config change between when a result was produced and when you fetch it cannot corrupt or misalign a page. Each page is an independently parseable JSON object `{"matches": [...], "evaluation_errors": [...]}` holding a whole-entry slice -- `json.loads` (or equivalent) works on every page by itself; concatenate every page's `matches`/`evaluation_errors` lists, in page order, to reconstruct the full arrays exactly, in original order.

When `truncated: false` (or absent), the full `matches[]` and `evaluation_errors[]` arrays are returned inline, unmodified.

**Cache degradation and failure** (Bug #1928 final round): `cache_unavailable: true` means PayloadCache was down when the result was built -- you still get a bounded, honest inline page 1 (`truncated: true`, `cache_handle: null`), just no further pages to fetch until the cache is back. `success: false, error: "cache_store_failed"` means the cache was reachable but the atomic page-set write itself failed; the job result still carries every non-array metadata field the search already produced, alongside the failure -- only `matches[]`/`evaluation_errors[]` are genuinely undeliverable.

## Iterating on Your Evaluator

1. Start with `max_results: 5` to test the evaluator on a small subset of candidate files. This prevents long waits during development and quickly reveals type or API mistakes.
2. Use `xray_explore` first to discover the AST shape produced by tree-sitter for the language. The `ast_debug` field shows the available node kinds and child structure so the evaluator can reference them correctly.
3. After each run, read `evaluation_errors` carefully:
   - `EvaluatorCrash` -- the evaluator code has a runtime error (e.g. calling a method that does not exist on OwnedNode, index out of bounds).
   - `EvaluatorTimeout` -- the evaluator is too slow or has an unbounded loop. The 5s sandbox timeout fired.
   - `ValidationFailed` -- the evaluator used a forbidden Rust construct. Check the `offending_construct` field to see what was rejected.
4. Once the evaluator runs cleanly on `max_results: 5`, remove the cap and run the full search.
5. Remember: `node.kind` (not `node.type`), `node.named_children()` (method call, not property), `node.descendants_of_kind(...)` (not `descendants_of_type`), `node.start_line` (already 1-based, not `start_point[0] + 1`).

## Pattern Library

Before writing `evaluator_code` inline, check the pattern library: a pattern for your use case may already exist (see "Discovering Available Patterns" below).

When an evaluator pattern is complex, tuned through iteration, or costly to produce, save it via `store_xray_pattern` for reuse. Stored patterns:

- Are referenced by name via the `pattern_name` parameter (mutually exclusive with `evaluator_code`).
- Support typed parameters with defaults, overridable per-call via `pattern_params`.
- Persist in cidx-meta (git-versioned) across sessions and server restarts.
- Seed patterns `catch-rethrow` and `deep-nesting` are created automatically in `__any__/` scope on first use.

**Recommendation**: If you have spent significant effort developing and testing an evaluator, store it rather than discarding it -- especially before ending a session, so the work survives session restart and reaches all users. Future searches can reference the pattern by name without reconstructing the evaluator logic.

### Discovering Available Patterns

List stored patterns using standard cidx-meta browsing tools:

- `browse_directory('cidx-meta-global', path='xray-patterns/__any__')` -- cross-repo patterns
- `browse_directory('cidx-meta-global', path='xray-patterns/{repo-alias}')` -- repo-specific patterns
- `get_file_content('cidx-meta-global', path='xray-patterns/__any__/{name}.yaml')` -- read pattern source

## Examples

**Find all function definitions (no AST filtering beyond node kind)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "def ",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for f in node.descendants_of_kind(\"function_definition\") {\n        findings.push(EvalFinding {\n            pattern: \"function_def\".to_string(),\n            line: f.start_line,\n            snippet: f.text().chars().take(80).collect(),\n        });\n    }\n    findings\n}",
  "search_target": "content",
  "include_patterns": ["*.py"]
}
```

**Test evaluator on 5 files before full search**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "prepareStatement",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for inv in node.descendants_of_kind(\"method_invocation\") {\n        if inv.text().contains(\"prepareStatement\") {\n            findings.push(EvalFinding {\n                pattern: \"prepare_statement\".to_string(),\n                line: inv.start_line,\n                snippet: inv.text().chars().take(80).collect(),\n            });\n        }\n    }\n    findings\n}",
  "search_target": "content",
  "max_results": 5
}
```

**Find Python test files by path pattern (filename target)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "test_.*\\.py$",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    vec![EvalFinding {\n        pattern: \"test_file\".to_string(),\n        line: 1,\n        snippet: node.text().chars().take(80).collect(),\n    }]\n}",
  "search_target": "filename"
}
```

**Search with include and exclude patterns (TODO/FIXME comments, skip vendored code)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "TODO|FIXME",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for c in node.descendants_of_kind(\"comment\") {\n        if c.text().contains(\"TODO\") || c.text().contains(\"FIXME\") {\n            findings.push(EvalFinding {\n                pattern: \"todo_fixme\".to_string(),\n                line: c.start_line,\n                snippet: c.text().chars().take(120).collect(),\n            });\n        }\n    }\n    findings\n}",
  "search_target": "content",
  "include_patterns": ["*.py", "*.java", "*.ts"],
  "exclude_patterns": ["*/vendor/*", "*/node_modules/*", "*/test/*"]
}
```

**Multi-repo (omni) search across two repos** -- returns `{job_ids: [...], errors: [...]}`:
```json
{
  "repository_alias": ["backend-global", "frontend-global"],
  "pattern": "TODO",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for c in node.descendants_of_kind(\"comment\") {\n        if c.text().contains(\"TODO\") {\n            findings.push(EvalFinding {\n                pattern: \"todo\".to_string(),\n                line: c.start_line,\n                snippet: c.text().chars().take(80).collect(),\n            });\n        }\n    }\n    findings\n}",
  "search_target": "content"
}
```

### Example: detect prepareStatement calls not in try-with-resources (Java)

Find every `prepareStatement(...)` call that is NOT inside a try-with-resources statement. Walk DOWN from try-with-resources blocks and check whether each method invocation falls within one.

```json
{
  "repository_alias": "myapp-global",
  "pattern": "prepareStatement",
  "search_target": "content",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    let try_blocks = node.descendants_of_kind(\"try_with_resources_statement\");\n    for inv in node.descendants_of_kind(\"method_invocation\") {\n        if inv.text().contains(\"prepareStatement\") {\n            let in_try = try_blocks.iter().any(|t| {\n                t.start_byte <= inv.start_byte && inv.end_byte <= t.end_byte\n            });\n            if !in_try {\n                findings.push(EvalFinding {\n                    pattern: \"no_try_with_resources\".to_string(),\n                    line: inv.start_line,\n                    snippet: inv.text().chars().take(80).collect(),\n                });\n            }\n        }\n    }\n    findings\n}",
  "include_patterns": ["*.java"]
}
```

The evaluator collects all `try_with_resources_statement` nodes first, then checks each `method_invocation` containing "prepareStatement" against those blocks by byte range. Invocations outside any try-with-resources block are reported as findings.

## Cancellation

Running xray_search jobs can be cancelled via `cancel_job(job_id)`. XRay jobs receive real process termination (SIGTERM, then SIGKILL after a 2-second grace period) rather than cooperative flag-only cancellation. The job status transitions to `cancelled`. Multi-repo searches return one job_id per repo -- cancel each individually.

## Related

- See `list_global_repos` to see available repositories before calling this tool.
- See `cancel_job` to cancel a running xray_search job with process termination.
- See `xray_explore` for verbose AST debug output to help craft evaluator code.
- `xray_explore` runs the same two-phase pipeline but adds an `ast_debug` field to every match, showing the complete tree-sitter AST node structure. Use it before writing your `evaluator_code`.
- See `cidx_fetch_cached_payload` to retrieve large truncated results by `cache_handle`.
