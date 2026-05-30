---
name: xray_explore
category: search
required_permission: query_repos
tl_dr: Debug-mode X-Ray search -- same two-phase driver+evaluator as xray_search but enriches every match with a serialised AST tree so you can understand the node structure tree-sitter produces for your code. Evaluator is Rust native code (fn evaluate_node).
slim_description: "Debug variant of xray_search: regex pattern narrows files, then Rust native evaluator (fn evaluate_node) runs against each file's tree-sitter AST, enriching matches with BFS-serialized ast_debug tree for evaluator development."
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
      - type: string
      - type: array
        items:
          type: string
      description: 'Repository identifier(s): String for single-repo exploration, array of strings for omni multi-repo exploration. JSON-encoded string arrays are also accepted. Use list_global_repos to see available repositories.'
    pattern:
      type: string
      description: 'Regular expression applied in Phase 1 to file content (search_target=content) or relative file paths (search_target=filename) to identify candidate files. Backed by RegexSearchService (ripgrep) for content. Renamed from driver_regex in v10.3.x.'
    evaluator_code:
      type: string
      description: 'Rust code defining fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>. The function receives the file root AST node (OwnedNode) and returns zero or more findings. Each EvalFinding has: pattern (String identifying the finding), line (usize, 1-based line number), snippet (String, code context). The OwnedNode and EvalFinding types are provided automatically by the compiler preamble -- do not define them yourself. Use node.descendants_of_kind("type_name") to walk the AST. Rust security whitelist enforced: no unsafe, no std::fs/net/process/env/io, no raw pointers, no extern blocks, no forbidden macros. When omitted, the server substitutes a default evaluator that produces one finding per Phase 1 hit (or a single file-level finding in filename mode), accepting all candidate files for AST exploration.'
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
      description: 'Enable multi-line regex matching in the Phase 1 content driver. Default false.'
      default: false
    pcre2:
      type: boolean
      description: 'Enable PCRE2 engine for advanced features (lookahead, lookbehind) in the Phase 1 content driver. Requires ripgrep built with PCRE2. Default false.'
      default: false
    timeout_seconds:
      type: integer
      description: 'Per-job wall-clock timeout in seconds. Range 10..600. Default 120.'
      minimum: 10
      maximum: 600
      default: 120
    max_debug_nodes:
      type: integer
      description: 'Maximum number of AST nodes to include in the ast_debug payload for each match. When the cap is hit a {"type": "...truncated"} sentinel appears in the children list. Range: 1..500. Default: 50.'
      minimum: 1
      maximum: 500
      default: 50
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
    pattern_name:
      type: string
      description: "Before writing evaluator_code inline, check the pattern library — a pattern for your use case may already exist. Use browse_directory('cidx-meta-global', path='xray-patterns') to list available patterns. Name of a stored xray evaluator pattern to use (from the cidx-meta pattern library). Mutually exclusive with evaluator_code — provide one or the other, not both. When provided, the server loads the pattern YAML, resolves typed parameter defaults, applies any pattern_params overrides, and uses the resulting evaluator code. Seed patterns catch-rethrow and deep-nesting are created automatically in __any__/ scope on first use. Use store_xray_pattern to add custom patterns. Before ending a session where you developed a new evaluator, if it took iteration, call store_xray_pattern so the work survives session restart and reaches all users."
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
      description: 'Multi-repo: per-alias resolution failures.'
    matches:
      type: array
      description: 'Inline match list when await_seconds resolves. Each entry has file_path, language, line_number, line_content, matched_node, ast_debug, plus pattern and snippet from the Rust evaluator findings.'
      items:
        type: object
    file_metadata:
      type: array
      description: 'Inline per-file metadata list when await_seconds resolves. One entry per evaluated file. Shape: {file_path}.'
      items:
        type: object
    is_admin:
      type: boolean
      description: 'Job-priority opt-in flag present in GET /api/jobs/{job_id} results. Always false for xray_explore and xray_search jobs -- these handlers never request the admin priority lane. This field does NOT reflect whether the submitting user is an administrator; an admin user submitting xray_explore will see is_admin=false.'
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

Developer-facing exploration tool for understanding AST node structure.

Use this tool when writing or debugging an `evaluator_code` snippet for `xray_search`. It runs the same two-phase pipeline as `xray_search` but adds an `ast_debug` field to every match showing the complete tree-sitter AST rooted at the matched file's parse root, plus a `matched_node` summary of that root.

For production search workflows use `xray_search` instead -- it is faster because it omits the AST serialisation overhead.

PHASE 1 (driver, regex): the `pattern` regex narrows the file set. For `search_target='content'`, RegexSearchService (ripgrep-backed) walks the repo and records every hit's line number, column, line content, and context lines. For `search_target='filename'`, an inline path walker matches relative file paths. Phase 1 honors `path`, `include_patterns`, `exclude_patterns`, `case_sensitive`, `multiline`, `pcre2`, and `context_lines`.

PHASE 2 (evaluator, AST): for each candidate file, tree-sitter parses the file once, then your `evaluator_code` runs as a Rust native evaluator (compiled to a dynamic library). The evaluator receives the file root AST node as an `OwnedNode` and returns `Vec<EvalFinding>` -- a list of findings, each with a pattern name, line number, and code snippet. The server enriches each finding with `file_path`, `language`, `line_content`, `matched_node`, and `ast_debug`.

Returns `{job_id}` (single repo) or `{job_ids, errors}` (multi-repo) immediately; poll `GET /api/jobs/{job_id}` for results, or set `await_seconds > 0` to inline-wait up to 120 seconds (v10.5.0).

## Quick Start

Discover AST node types for Java files -- omit `evaluator_code` to accept all Phase 1 hits and get their full AST structure:

```json
{
  "repository_alias": "my-repo-global",
  "pattern": "prepareStatement",
  "search_target": "content",
  "include_patterns": ["*.java"],
  "max_results": 2,
  "max_debug_nodes": 30,
  "await_seconds": 10
}
```

Then use the `ast_debug` payload in each match to identify node type names (e.g. `method_invocation`, `try_with_resources_statement`) and write a targeted evaluator for `xray_search`. Use `debug_log()` to trace evaluator logic during development:

```json
{
  "repository_alias": "my-repo-global",
  "pattern": "def ",
  "search_target": "content",
  "include_patterns": ["*.py"],
  "max_results": 3,
  "max_debug_nodes": 20,
  "await_seconds": 10,
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    debug_log(&format!(\"root kind: {}\", node.kind));\n    let funcs = node.descendants_of_kind(\"function_definition\");\n    debug_log(&format!(\"found {} functions\", funcs.len()));\n    funcs.iter().map(|f| EvalFinding {\n        pattern: \"func\".to_string(),\n        line: f.start_line,\n        snippet: truncate_snippet(&f.text, 80),\n    }).collect()\n}"
}
```

Key points:
- **Evaluator code is Rust**, not Python. Signature: `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`.
- `OwnedNode`, `EvalFinding`, `truncate_snippet`, and `debug_log` are provided automatically.
- Omit `evaluator_code` to explore AST structure without writing any Rust -- the default evaluator accepts all files.
- Keep `max_results` and `max_debug_nodes` small while iterating -- AST payloads are large.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str OR list[str] | yes | -- | Single alias or array (or JSON-encoded array string) for omni multi-repo exploration. |
| pattern | str | yes | -- | Regular expression applied in Phase 1. Renamed from `driver_regex` in v10.3.x. |
| evaluator_code | str | no | (default acceptor) | Rust code defining `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`. See Evaluator API below. When omitted, default accepts all files for AST exploration. |
| search_target | "content" or "filename" | yes | -- | "content" -- Phase 1 regex applies to file text. "filename" -- Phase 1 regex applies to relative paths. |
| include_patterns | list[str] | no | [] | Glob patterns for files to include. `*` matches a single path segment; use `**` for recursive matching. Empty means include all. |
| exclude_patterns | list[str] | no | [] | Glob patterns for files to exclude. Empty means exclude none. |
| path | str | no | null | Subdirectory restriction within the repo (relative). |
| case_sensitive | bool | no | true | Phase 1 content driver case sensitivity. |
| context_lines | int | no | 0 | Lines of context before/after each Phase 1 hit (0..10). |
| multiline | bool | no | false | Multi-line regex matching in the content driver. |
| pcre2 | bool | no | false | PCRE2 engine for the content driver. |
| timeout_seconds | int | no | 120 | Per-job wall-clock cap (10..600). |
| max_debug_nodes | int | no | 50 | Maximum AST nodes in the `ast_debug` payload per match (1..500). When the cap is hit a `{"type": "...truncated"}` sentinel appears in the children list. |
| max_results | int | no | null | Cap on candidate files evaluated. When hit: `partial=true`, `max_files_reached=true`. Renamed from `max_files` in v10.3.x. |
| await_seconds | float | no | 0 | Server-side inline-wait window (0.0..120.0, v10.5.0). |
| pattern_name | str | no | null | Name of a stored xray evaluator pattern (from the cidx-meta library). Mutually exclusive with `evaluator_code`. When provided, the server loads and resolves the pattern, applying `pattern_params` overrides. Error `mutually_exclusive_params` if both are provided. |
| pattern_params | object | no | null | Parameter overrides for the resolved pattern. Only valid when `pattern_name` is provided. Keys must match declared parameter names (UPPER_SNAKE_CASE); values must be type-compatible. |

## Evaluator API

The evaluator API for `xray_explore` is identical to `xray_search`. The evaluator runs ONCE per file (file-as-unit contract).

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
    pub pattern: String,  // pattern name identifying the finding
    pub line: usize,      // 1-based line number where the finding occurs
    pub snippet: String,  // code snippet providing context
}
```

The server enriches each finding additionally with `matched_node` and `ast_debug` for `xray_explore`.

### debug_log() function

Use `debug_log(msg: &str)` inside your evaluator to trace execution. Debug messages appear in `debug_output[]` in the result, making it easy to understand why the evaluator produces or skips findings.

```rust
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    debug_log(&format!("evaluating: kind={}, children={}", node.kind, node.children.len()));
    vec![]
}
```

**Limits**: 100 messages max, 10 KB total. Messages past either limit are silently dropped. When no `debug_log()` calls are made, `debug_output` is an empty list (zero overhead).

### OwnedNode reference

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

For the full 15-pattern cookbook of worked evaluator examples and a cross-language node type table covering the 15 mandatory languages (java, kotlin, go, python, typescript, javascript, bash, csharp, html, css, hcl/terraform, yaml, sql, xml, groovy), see the corresponding sections in `xray_search`. The same evaluator API and `OwnedNode` surface apply to both tools.

### Globals available in evaluator context

The Rust evaluator receives the file root as `node`. The following names from the former Python XRayNode API are documented here for migration reference -- they are NOT available in the Rust evaluator. Callers who previously used the Python API (which exposed `node`, `root`, `source`, `lang`, `file_path`, and `match_positions` as globals) should migrate to the Rust `OwnedNode` API described above.

The former Python API accepted evaluator code returning a dict with keys `{"matches": [...], "value": <any>}`. Node types were Python AST class names such as `Call`, `Name`, `Attribute`, `Constant`, `Subscript`, `Compare`, `BoolOp`, `UnaryOp`, `List`, `Tuple`, `Dict`, `Return`, `Expr`. The execution environment stripped builtins including `getattr`, `setattr`, `delattr`, `hasattr`, `__import__`, `eval`, `exec`, `open`, and `compile`. Dunder attribute access (e.g. `__class__`, `__bases__`, `__globals__`, `__builtins__`, `__dict__`) was blocked as sandbox escape vectors. This Python evaluator API was retired in Epic #1019 and replaced by the Rust native evaluator.

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

### ast_debug Payload

Each match in the polled result includes an `ast_debug` field with a breadth-first serialised AST tree capped at `max_debug_nodes` nodes:

```json
{
  "type": "module",
  "start_byte": 0,
  "end_byte": 128,
  "start_point": [0, 0],
  "end_point": [5, 0],
  "text_preview": "class Foo:\n    def bar(self)...",
  "child_count": 1,
  "children": [
    {
      "type": "class_definition",
      "start_byte": 0,
      "end_byte": 128,
      "start_point": [0, 0],
      "end_point": [4, 20],
      "text_preview": "class Foo:\n    def bar(self)...",
      "child_count": 3,
      "children": [
        {"type": "...truncated"}
      ]
    }
  ]
}
```

Fields per node:
- `type` -- tree-sitter node type string (e.g. "method_invocation", "identifier")
- `start_byte` / `end_byte` -- byte offsets into the source file
- `start_point` / `end_point` -- [row, col] line/column positions (0-indexed)
- `text_preview` -- first 80 characters of the node's source text (UTF-8)
- `child_count` -- total direct children in the full tree (may exceed children list size when truncated)
- `children` -- serialised child nodes; contains `{"type": "...truncated"}` when the `max_debug_nodes` cap is hit

### Evaluator Safety

The sandbox enforces security at two stages:
1. Rust AST whitelist -- rejects any forbidden construct (unsafe, std::fs/net/process/env/io, raw pointers, extern, forbidden macros) before compilation.
2. 5-second hard timeout with process termination enforced via the sandbox runtime.

### Polled Result Shape

After polling `GET /api/jobs/{job_id}` to COMPLETED status, `result` contains:

- `matches[]`: list of enriched match dicts. Every entry contains:
  - `file_path` (str, server-added) -- relative path from the repository root
  - `language` (str, server-added) -- tree-sitter language name
  - `line_number` (int, from EvalFinding.line) -- 1-based line number
  - `line_content` (str, server-derived) -- raw text of `line_number` from the file source
  - `pattern` (str, from EvalFinding.pattern) -- the pattern name identifying the finding
  - `snippet` (str, from EvalFinding.snippet) -- code snippet context from the evaluator
  - `matched_node` (server-added): compact description of the file root node -- `{type, start_byte, end_byte, start_point, end_point}`. Distinct from the `node` evaluator input only in serialisation; both refer to the file root.
  - `ast_debug` (server-added): full BFS-serialised AST tree rooted at the file root (see "ast_debug Payload" above).
- `file_metadata[]`: per-file metadata entries. One entry per evaluated file. Shape: `{file_path}`.
- `evaluation_errors[]`: list of per-file failures. Each entry: `{file_path, line_number, error_type, error_message}`. `evaluation_errors` does NOT cause job failure -- status remains COMPLETED.
- `files_processed` (int): number of candidate files evaluated.
- `files_total` (int): total candidate files found by Phase 1.
- `elapsed_seconds` (float)
- `partial: true` (only on partial completion)
- `timeout: true` (only when job-level timeout fired -- takes precedence over `max_files_reached`)
- `max_files_reached: true` (only when the `max_results` cap fired before timeout)
- `warnings[]` (only when present): zero-match include_pattern hints

### evaluation_errors[] payload examples

`xray_explore` reuses the same pipeline as `xray_search`, so the `evaluation_errors[]` shape is identical:

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
  "file_path": "src/code_indexer/handlers/edge_case.py",
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

**Generic exception types** (e.g. `IOError`, `AttributeError`, `UnicodeDecodeError`) -- emitted by the catch-all when the file cannot be read or parsed. The `error_type` is the exception class name; `error_message` is the error detail string.

### Large Result Paging

`xray_explore` results are typically much larger than `xray_search` results because every match carries an `ast_debug` payload of up to `max_debug_nodes` AST nodes. Truncation kicks in earlier -- keep `max_results` and `max_debug_nodes` small while iterating.

For results larger than ~2000 chars (configurable via Web UI `payload_preview_size_chars`), the polled job result is truncated and stored in PayloadCache:

- `truncated: true` -- set when the matches+errors JSON exceeded the preview cap
- `has_more: true` -- synonym; set with `truncated`
- `cache_handle: "<uuid>"` -- opaque handle for paged retrieval
- `total_size: <int>` -- full payload byte size
- `matches_and_errors_preview: "<first 2000 chars of JSON>"` -- quick preview
- `matches[]` and `evaluation_errors[]` -- only the first 3 entries inline

To fetch the full content: `GET /api/cache/{cache_handle}` (paged via `?page=N`), or use the discoverable `cidx_fetch_cached_payload` MCP tool.

### Examples

**Explore AST structure for Java files calling prepareStatement** (default evaluator accepts every Phase 1 hit):
```json
{
  "repository_alias": "backend-global",
  "pattern": "prepareStatement",
  "search_target": "content",
  "include_patterns": ["*.java"],
  "max_results": 3,
  "max_debug_nodes": 30
}
```

**Explore first 2 Python test files to understand AST shape** (filename target):
```json
{
  "repository_alias": "backend-global",
  "pattern": "test_.*\\.py$",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    vec![EvalFinding {\n        pattern: \"test_file\".to_string(),\n        line: 1,\n        snippet: node.text.chars().take(80).collect(),\n    }]\n}",
  "search_target": "filename",
  "max_results": 2,
  "max_debug_nodes": 50
}
```

**Explore matches with custom evaluator** (compute complexity per function):
```json
{
  "repository_alias": "backend-global",
  "pattern": "def ",
  "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut findings = Vec::new();\n    for f in node.descendants_of_kind(\"function_definition\") {\n        let cx = f.descendants_of_kind(\"if_statement\").len() + f.descendants_of_kind(\"elif_clause\").len();\n        findings.push(EvalFinding {\n            pattern: \"complexity\".to_string(),\n            line: f.start_line,\n            snippet: format!(\"complexity={}\", cx),\n        });\n    }\n    findings\n}",
  "search_target": "content",
  "include_patterns": ["*.py"],
  "max_results": 5,
  "max_debug_nodes": 20
}
```

## Pattern Library

When an evaluator pattern is complex, tuned through iteration, or costly to produce, save it via `store_xray_pattern` for reuse. Stored patterns:

- Are referenced by name via the `pattern_name` parameter (mutually exclusive with `evaluator_code`).
- Support typed parameters with defaults, overridable per-call via `pattern_params`.
- Persist in cidx-meta (git-versioned) across sessions and server restarts.

**Recommendation**: If you have spent significant effort developing and testing an evaluator with `xray_explore`, store it rather than discarding it. Future searches (via `xray_explore` or `xray_search`) can reference the pattern by name without reconstructing the evaluator logic.

### Discovering Available Patterns

List stored patterns using standard cidx-meta browsing tools:

- `browse_directory('cidx-meta-global', path='xray-patterns/__any__')` -- cross-repo patterns
- `browse_directory('cidx-meta-global', path='xray-patterns/{repo-alias}')` -- repo-specific patterns
- `get_file_content('cidx-meta-global', path='xray-patterns/__any__/{name}.yaml')` -- read pattern source

## Cancellation

Running xray_explore jobs can be cancelled via `cancel_job(job_id)`. XRay jobs receive real process termination (SIGTERM, then SIGKILL after a 2-second grace period) rather than cooperative flag-only cancellation. The job status transitions to `cancelled`. Multi-repo searches return one job_id per repo -- cancel each individually.

## Related

- See `cancel_job` to cancel a running xray_explore job with process termination.
- See `xray_search` for the production search variant (no AST debug overhead).
- See `xray_dump_ast` for a synchronous single-file AST dump (no Phase 1 driver, no evaluator).
- See `cidx_fetch_cached_payload` to retrieve large truncated results by `cache_handle`.
