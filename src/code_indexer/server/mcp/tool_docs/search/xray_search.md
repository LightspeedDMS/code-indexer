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
      description: 'Repository identifier(s): String for single-repo search, array of strings for omni multi-repo search across multiple repos. JSON-encoded string arrays (e.g. ''["repo-a","repo-b"]'') are also accepted and parsed as arrays. Single-repo returns {job_id}; multi-repo returns {job_ids, errors}. Use list_global_repos to see available repositories.'
    pattern:
      type: string
      description: 'Regular expression applied in Phase 1 to file content (search_target=content) or relative file paths (search_target=filename) to identify candidate files. Backed by RegexSearchService (ripgrep) for content. Renamed from driver_regex in v10.3.x.'
    evaluator_code:
      type: string
      description: 'Rust code defining fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>. The function receives the file root AST node (OwnedNode) and returns zero or more findings. Each EvalFinding has: pattern (String identifying the finding), line (usize, 1-based line number), snippet (String, code context). The OwnedNode and EvalFinding types are provided automatically by the compiler preamble -- do not define them yourself. Use node.descendants_of_kind("type_name") to walk the AST. Rust security whitelist enforced: no unsafe, no std::fs/net/process/env/io, no raw pointers, no extern blocks, no forbidden macros.'
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
      description: 'Glob patterns for files to include (e.g. ["*.java", "*.kt"]). "*" matches a single path segment; use "**" for recursive segment matching. Empty list means include all.'
      default: []
    exclude_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to exclude (e.g. ["*/test/*"]). Empty list means exclude none.'
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
      description: 'Maximum number of candidate files to evaluate. When the cap is hit the result includes partial=true and max_files_reached=true. Use a small value (e.g. 5) to test the evaluator before running the full search. Renamed from max_files in v10.3.x. Must be >= 1 when provided.'
      minimum: 1
    await_seconds:
      type: number
      description: 'Optional server-side polling window in seconds. Accepts floats (e.g. 2.5). When 0 (default), returns {job_id} immediately. When > 0, the server polls the background job for up to await_seconds and returns the inline result if the job completes; otherwise falls back to {job_id}. Range 0.0..120.0 (raised from 10.0 in v10.5.0). Values > 30.0 emit a server-side warning. Error code await_seconds_invalid if out of range or wrong type.'
      minimum: 0
      maximum: 120.0
      default: 0
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

Returns `{job_id}` (single repo) or `{job_ids, errors}` (multi-repo) immediately; poll `GET /api/jobs/{job_id}` for results, or set `await_seconds > 0` to inline-wait up to 120 seconds (v10.5.0).

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str OR list[str] | yes | -- | Single alias for single-repo search. Array (or JSON-encoded array string) for omni multi-repo search. Multi-repo returns one job id per resolved alias plus an `errors[]` list for unresolved aliases. |
| pattern | str | yes | -- | Regular expression applied in Phase 1. Renamed from `driver_regex` in v10.3.x. |
| evaluator_code | str | no | (default acceptor) | Rust code defining `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`. See "Evaluator API" below. When omitted, the server substitutes a default that produces one finding per Phase 1 hit. |
| search_target | "content" or "filename" | yes | -- | "content" -- Phase 1 regex applies to file text. "filename" -- Phase 1 regex applies to relative paths. |
| include_patterns | list[str] | no | [] | Glob patterns for files to include. `*` matches a single path segment; use `**` for recursive matching. Empty means include all. |
| exclude_patterns | list[str] | no | [] | Glob patterns for files to exclude. Empty means exclude none. |
| path | str | no | null | Subdirectory restriction within the repo (relative). |
| case_sensitive | bool | no | true | Phase 1 content driver case sensitivity. |
| context_lines | int | no | 0 | Lines of context before/after each Phase 1 hit. Range 0..10. |
| multiline | bool | no | false | Multi-line regex matching in the content driver. |
| pcre2 | bool | no | false | PCRE2 engine for the content driver (lookahead/lookbehind). |
| timeout_seconds | int | no | 120 | Per-job wall-clock cap. Range 10..600. |
| max_results | int | no | null | Cap on candidate files evaluated. When hit: `partial=true`, `max_files_reached=true`. Renamed from `max_files` in v10.3.x. |
| await_seconds | float | no | 0 | Server-side inline-wait window. 0 = return job id immediately. Range 0.0..120.0 (v10.5.0). Values > 30.0 emit a server warning. |

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
            snippet: f.text.chars().take(80).collect(),
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

- **Input**: `node: &OwnedNode` -- the root node of the file's tree-sitter parse tree. Access the full source text via `node.text`. Walk descendant nodes via `node.descendants_of_kind(...)`.
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
- `snippet`: code context for the finding. Typically the node's text truncated to a reasonable length: `node.text.chars().take(80).collect()`.

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
| `node.text` | String | raw source text of this node |
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
- The `truncate_snippet(s: &str, max_len: usize) -> String` utility (provided by compiler preamble) -- collapses whitespace and truncates to `max_len` bytes on a UTF-8 boundary, appending "..." if truncated

> **Termination guarantee**: infinite loops in your evaluator hit the sandbox hard timeout (HARD_TIMEOUT_SECONDS = 5.0 s) and surface as `EvaluatorTimeout` in `evaluation_errors[]`. The sandbox timeout is the authoritative termination boundary.

### Cookbook: 15 worked patterns

Each example is a complete `evaluator_code` value. All patterns return `Vec<EvalFinding>`. Structure evaluators by walking DOWN from the root node using `descendants_of_kind`.

1. **Find all function definitions in each file**:
   ```rust
   fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
       let mut findings = Vec::new();
       for f in node.descendants_of_kind("function_definition") {
           findings.push(EvalFinding {
               pattern: "function_def".to_string(),
               line: f.start_line,
               snippet: f.text.chars().take(80).collect(),
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
                   snippet: call.text.chars().take(80).collect(),
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
           if call.text.contains(target_name) {
               findings.push(EvalFinding {
                   pattern: "target_call".to_string(),
                   line: call.start_line,
                   snippet: call.text.chars().take(80).collect(),
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
           let text = s.text.to_string();
           if text.contains("SELECT") || text.contains("select") {
               findings.push(EvalFinding {
                   pattern: "sql_string".to_string(),
                   line: s.start_line,
                   snippet: s.text.chars().take(80).collect(),
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
                    snippet: ret.text.chars().take(80).collect(),
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
                    snippet: call.text.chars().take(80).collect(),
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
            if c.text.contains("TODO") || c.text.contains("FIXME") {
                findings.push(EvalFinding {
                    pattern: "todo_fixme".to_string(),
                    line: c.start_line,
                    snippet: c.text.chars().take(120).collect(),
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
            let name_text = &children[0].text;
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
                    snippet: e.text.chars().take(80).collect(),
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
                children[0].text.clone()
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

### Common cross-language node type names

tree-sitter node type names differ between languages. Use this table as a starting reference for the 7 general-purpose mandatory languages. Bash, HTML, and CSS are also supported but have limited overlap with the constructs below -- use `xray_dump_ast` to discover their node types.

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

The fastest way to discover the exact type names for a construct is to use `xray_dump_ast` on a small example file in the language, or consult the tree-sitter grammar repository for that language.

## Polled Result Shape

After polling `GET /api/jobs/{job_id}` to COMPLETED status, `result` contains:

- `matches[]`: list of enriched match dicts. Every entry contains:
  - `file_path` (str, server-added) -- absolute path
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
- `warnings[]` (only when present): zero-match include_pattern hints

### evaluation_errors[] payload examples

Each `error_type` carries a distinct `error_message` shape:

**EvaluatorTimeout** -- sandbox 5s wall-clock budget exceeded:

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/server/services/very_large_module.py",
  "line_number": 0,
  "error_type": "EvaluatorTimeout",
  "error_message": "evaluator exceeded 5s sandbox limit"
}
```

**EvaluatorCrash** -- evaluator process died before returning a value. The `error_message` carries the failure detail:

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/cli.py",
  "line_number": 0,
  "error_type": "EvaluatorCrash",
  "error_message": "evaluator process exited with non-zero status"
}
```

**UnsupportedLanguage** -- Phase 1 selected a candidate whose extension has no tree-sitter grammar:

```json
{
  "file_path": "/srv/cidx/repo/docs/architecture.md",
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

For results larger than ~2000 chars (configurable via Web UI `payload_preview_size_chars`), the polled job result is truncated and stored in PayloadCache. Additional fields:

- `truncated: true` -- set when the matches+errors JSON exceeded the preview cap
- `has_more: true` -- synonym; set with `truncated`
- `cache_handle: "<uuid>"` -- opaque handle for paged retrieval
- `total_size: <int>` -- full payload byte size
- `matches_and_errors_preview: "<first 2000 chars of JSON>"` -- quick preview
- `matches[]` and `evaluation_errors[]` -- only the first 3 entries inline

To fetch the full content: `GET /api/cache/{cache_handle}` (paged via `?page=N`), or use the discoverable `cidx_fetch_cached_payload` MCP tool.

When `truncated: false` (or absent), the full `matches[]` and `evaluation_errors[]` arrays are returned inline.

## Iterating on Your Evaluator

1. Start with `max_results: 5` to test the evaluator on a small subset of candidate files. This prevents long waits during development and quickly reveals type or API mistakes.
2. Use `xray_explore` first to discover the AST shape produced by tree-sitter for the language. The `ast_debug` field shows the available node kinds and child structure so the evaluator can reference them correctly.
3. After each run, read `evaluation_errors` carefully:
   - `EvaluatorCrash` -- the evaluator code has a runtime error (e.g. calling a method that does not exist on OwnedNode, index out of bounds).
   - `EvaluatorTimeout` -- the evaluator is too slow or has an unbounded loop. The 5s sandbox timeout fired.
   - `ValidationFailed` -- the evaluator used a forbidden Rust construct. Check the `offending_construct` field to see what was rejected.
4. Once the evaluator runs cleanly on `max_results: 5`, remove the cap and run the full search.
5. Remember: `node.kind` (not `node.type`), `node.named_children()` (method call, not property), `node.descendants_of_kind(...)` (not `descendants_of_type`), `node.start_line` (already 1-based, not `start_point[0] + 1`).

## Examples

**Find all function definitions (no AST filtering beyond node kind)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "def ",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for f in node.descendants_of_kind(\"function_definition\") {\n        findings.push(EvalFinding {\n            pattern: \"function_def\".to_string(),\n            line: f.start_line,\n            snippet: f.text.chars().take(80).collect(),\n        });\n    }\n    findings\n}",
  "search_target": "content",
  "include_patterns": ["*.py"]
}
```

**Test evaluator on 5 files before full search**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "prepareStatement",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for inv in node.descendants_of_kind(\"method_invocation\") {\n        if inv.text.contains(\"prepareStatement\") {\n            findings.push(EvalFinding {\n                pattern: \"prepare_statement\".to_string(),\n                line: inv.start_line,\n                snippet: inv.text.chars().take(80).collect(),\n            });\n        }\n    }\n    findings\n}",
  "search_target": "content",
  "max_results": 5
}
```

**Find Python test files by path pattern (filename target)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "test_.*\\.py$",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    vec![EvalFinding {\n        pattern: \"test_file\".to_string(),\n        line: 1,\n        snippet: node.text.chars().take(80).collect(),\n    }]\n}",
  "search_target": "filename"
}
```

**Search with include and exclude patterns (TODO/FIXME comments, skip vendored code)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "TODO|FIXME",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for c in node.descendants_of_kind(\"comment\") {\n        if c.text.contains(\"TODO\") || c.text.contains(\"FIXME\") {\n            findings.push(EvalFinding {\n                pattern: \"todo_fixme\".to_string(),\n                line: c.start_line,\n                snippet: c.text.chars().take(120).collect(),\n            });\n        }\n    }\n    findings\n}",
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
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for c in node.descendants_of_kind(\"comment\") {\n        if c.text.contains(\"TODO\") {\n            findings.push(EvalFinding {\n                pattern: \"todo\".to_string(),\n                line: c.start_line,\n                snippet: c.text.chars().take(80).collect(),\n            });\n        }\n    }\n    findings\n}",
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
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    let try_blocks = node.descendants_of_kind(\"try_with_resources_statement\");\n    for inv in node.descendants_of_kind(\"method_invocation\") {\n        if inv.text.contains(\"prepareStatement\") {\n            let in_try = try_blocks.iter().any(|t| {\n                t.start_byte <= inv.start_byte && inv.end_byte <= t.end_byte\n            });\n            if !in_try {\n                findings.push(EvalFinding {\n                    pattern: \"no_try_with_resources\".to_string(),\n                    line: inv.start_line,\n                    snippet: inv.text.chars().take(80).collect(),\n                });\n            }\n        }\n    }\n    findings\n}",
  "include_patterns": ["*.java"]
}
```

The evaluator collects all `try_with_resources_statement` nodes first, then checks each `method_invocation` containing "prepareStatement" against those blocks by byte range. Invocations outside any try-with-resources block are reported as findings.

## Cancellation

Running xray_search jobs can be cancelled via `cancel_job(job_id)`. XRay jobs receive real process termination (SIGTERM, then SIGKILL after a 2-second grace period) rather than cooperative flag-only cancellation. The job status transitions to `cancelled`. Multi-repo searches return one job_id per repo -- cancel each individually.

## Related

- See `cancel_job` to cancel a running xray_search job with process termination.
- See `xray_explore` for verbose AST debug output to help craft evaluator code.
- `xray_explore` runs the same two-phase pipeline but adds an `ast_debug` field to every match, showing the complete tree-sitter AST node structure. Use it before writing your `evaluator_code`.
- See `cidx_fetch_cached_payload` to retrieve large truncated results by `cache_handle`.
