---
name: xray_explore
category: search
required_permission: query_repos
tl_dr: Debug-mode X-Ray search — same two-phase driver+evaluator as xray_search but enriches every match with a serialised AST tree so you can understand the node structure tree-sitter produces for your code.
inputSchema:
  type: object
  properties:
    repository_alias:
      type: string
      description: 'Global repository alias, e.g. "myrepo-global". Use list_global_repos to see available repositories.'
    driver_regex:
      type: string
      description: 'Regular expression applied in Phase 1 to file content or file paths to identify candidate files. Phase 1 match collection is capped at 100,000 regex hits across the repository.'
    evaluator_code:
      type: string
      description: 'Optional. Python expression evaluated in a sandboxed subprocess against each AST match position. Must return a bool. Defaults to "return True" (accept all candidate files for AST exploration). For search_target=content, node is the deepest AST node enclosing the regex match position; for search_target=filename, node equals root. Available names: node, root, source, lang, file_path.'
    search_target:
      type: string
      enum:
        - content
        - filename
      description: 'What the driver_regex applies to: "content" matches against file text, "filename" matches against relative file paths.'
    include_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to include (e.g. ["*.java", "*.kt"]). Empty list means include all.'
      default: []
    exclude_patterns:
      type: array
      items:
        type: string
      description: 'Glob patterns for files to exclude (e.g. ["*/test/*"]). Empty list means exclude none.'
      default: []
    timeout_seconds:
      type: integer
      description: 'Per-job wall-clock timeout in seconds. Range: 10..600. Defaults to server config xray_timeout_seconds (120).'
      minimum: 10
      maximum: 600
      default: 120
    max_debug_nodes:
      type: integer
      description: 'Maximum number of AST nodes to include in the ast_debug payload for each match. When the cap is hit a {"type": "...truncated"} sentinel appears in the children list. Range: 1..500. Default: 50.'
      minimum: 1
      maximum: 500
      default: 50
    max_files:
      type: integer
      description: 'Maximum number of candidate files to evaluate. When the cap is hit the result includes partial=true and max_files_reached=true. Use a small value (e.g. 5) to test your evaluator before running the full search. Must be >= 1 when provided.'
      minimum: 1
  required:
    - repository_alias
    - driver_regex
    - search_target
outputSchema:
  type: object
  properties:
    job_id:
      type: string
      description: 'Background job identifier. Poll GET /api/jobs/{job_id} for progress and results.'
    error:
      type: string
      description: 'Error code when the request is rejected synchronously.'
    message:
      type: string
      description: 'Human-readable description of the error.'
---

Developer-facing exploration tool for understanding AST node structure. Returns {job_id} immediately; poll GET /api/jobs/{job_id} for results.

Use this tool when writing or debugging an evaluator_code expression for xray_search. It runs the same two-phase search as xray_search but adds an ast_debug field to every match showing the complete tree-sitter AST rooted at the matched file's parse root.

For production search workflows use xray_search instead — it is faster because it omits the AST serialisation overhead.

PHASE 1 (driver): regex driver narrows the file set — only files whose content (or path) matches driver_regex are passed to Phase 2.

PHASE 2 (evaluator): for each candidate file, the AST is parsed with tree-sitter and your Python evaluator_code runs in a sandboxed subprocess with access to: node (root XRayNode), root (root XRayNode), source (full file text), lang (language string), file_path (absolute path). Return True to include the file in matches.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str | yes | -- | Global repository alias, e.g. "myrepo-global". Use list_global_repos to see available repositories. |
| driver_regex | str | yes | -- | Regular expression applied in Phase 1 to file content or file paths to identify candidate files. Phase 1 match collection is capped at 100,000 regex hits across the repository. For very dense patterns on large repos, narrow the candidate set with include_patterns/exclude_patterns to avoid silent truncation. |
| evaluator_code | str | no | "return True" | Optional Python expression evaluated in a sandboxed subprocess against each AST match position. Must return a bool. Defaults to "return True" (accept all candidate files for AST exploration). For search_target=content, node is the deepest AST node enclosing the regex match position; for search_target=filename, node equals root. |
| search_target | "content" or "filename" | yes | -- | What the driver_regex applies to: "content" matches against file text (evaluator called once per match position with the enclosing AST node); "filename" matches against relative file paths (evaluator called once per file with node==root). |
| include_patterns | list[str] | no | [] | Glob patterns for files to include (e.g. ["*.java", "*.kt"]). Empty list means include all. |
| exclude_patterns | list[str] | no | [] | Glob patterns for files to exclude (e.g. ["*/test/*"]). Empty list means exclude none. |
| timeout_seconds | int | no | 120 | Per-job wall-clock timeout in seconds. Range: 10..600. Defaults to server config xray_timeout_seconds. |
| max_debug_nodes | int | no | 50 | Maximum number of AST nodes to include in the ast_debug payload for each match. Range: 1..500. When the cap is hit a {"type": "...truncated"} sentinel appears in the children list. |
| max_files | int | no | null | Maximum number of candidate files to evaluate. When the cap is hit the result includes partial=true and max_files_reached=true. Use max_files: 5 to test your evaluator before running the full search. |

## Evaluator API

The evaluator code runs inside a sandboxed subprocess and receives these names:

- `node` — for `search_target='content'`, the deepest XRayNode whose [start_byte, end_byte) contains the Phase 1 regex match position (the "closest enclosing ancestor" at the match site). For `search_target='filename'`, this is the file root (same as `root`). Use `node` to inspect the specific construct at the match; use `root` to traverse the entire file tree.
- `root` — the file's root XRayNode (tree-sitter parse root)
- `source` — the full file content as a UTF-8 string
- `lang` — one of: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css
- `file_path` — absolute path of the file being evaluated

For `search_target='content'`: the evaluator is called once per regex match position; `node` is the deepest AST node enclosing that position. For `search_target='filename'`: the evaluator is called once per matching file with `node == root` (no per-position semantics — there is no in-content match position to enclose).

Whitelisted Python AST node types (all others are rejected before any subprocess is spawned):
Call, Name, Attribute, Constant, Subscript, Compare, BoolOp, UnaryOp, List, Tuple, Dict, Return, Expr.
Abstract operator base classes (boolop, cmpop, unaryop, expr_context) and Module/Load markers are also accepted via isinstance().

Safe builtins (available in the exec() environment):
len, str, int, bool, list, tuple, dict, min, max, sum, any, all, range, enumerate, zip, sorted, reversed, hasattr.

Stripped builtins (removed from the exec() environment):
getattr, setattr, delattr, __import__, eval, exec, open, compile.

Dunder attribute blocklist (Attribute and Subscript access to these 39 names is rejected at AST validation time as sandbox escape vectors):

- Class introspection: `__class__, __bases__, __base__, __mro__, __subclasses__`
- Constructor / lifecycle: `__init__, __init_subclass__, __new__`
- Module/globals access: `__globals__, __builtins__, __import__, __module__, __loader__, __spec__, __file__, __path__, __package__, __cached__`
- Object internals: `__dict__, __getattribute__, __setattr__, __delattr__`
- Reduce / pickling: `__reduce__, __reduce_ex__`
- Function attrs: `__call__, __code__, __closure__, __func__, __defaults__, __kwdefaults__, __annotations__`
- Naming: `__name__, __qualname__`
- Modern features: `__type_params__`
- Descriptor / metaclass: `__set_name__, __instancecheck__, __subclasscheck__, __prepare__`
- Memory: `__weakref__`

Any attribute name matching `__*__` is also blocked at Subscript level (e.g. `globals()['__import__']`).

Hard timeout: 5 seconds per evaluator invocation, enforced by multiprocessing.Process isolation (SIGTERM at 5s, SIGKILL at 6s).

See xray_search for the production search variant. xray_explore is for development and AST shape discovery only.

### ast_debug Payload

Each match in the polled result includes an ast_debug field with a breadth-first serialised AST tree capped at max_debug_nodes nodes:

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
- type: tree-sitter node type string (e.g. "method_invocation", "identifier")
- start_byte / end_byte: byte offsets into the source file
- start_point / end_point: [row, col] line/column positions (0-indexed)
- text_preview: first 80 characters of the node's source text (UTF-8)
- child_count: total number of direct children in the full tree (may exceed children list size when truncated)
- children: serialised child nodes; contains {"type": "...truncated"} when max_debug_nodes cap is hit

### Evaluator Safety

The sandbox enforces three defence layers:
1. AST whitelist — rejects Import, For, While, With, and other non-expression nodes before any subprocess is spawned.
2. Stripped builtins — exec() environment removes getattr, setattr, open, eval, exec, compile, __import__.
3. 5-second hard timeout with SIGTERM + SIGKILL escalation.

### Polled Result Shape

After polling GET /api/jobs/{job_id} to COMPLETED status, result contains:

- matches[]: file_path, line_number, code_snippet, language, evaluator_decision, ast_debug
  - Note: line_number and code_snippet are null until story #978 wires per-match position tracking via RegexSearchService.
- evaluation_errors[]: list of per-match evaluator failures. Each entry has file_path, line_number, error_type, error_message. error_type is one of: AttributeError (evaluator referenced a node attribute that does not exist for this node type), EvaluatorTimeout (the 5s sandbox timer fired), EvaluatorCrash (subprocess exited with non-zero code), UnsupportedLanguage, NonBoolReturn. evaluation_errors does NOT cause job failure — status remains COMPLETED.
- files_processed: int — number of candidate files evaluated
- files_total: int — total candidate files found by driver
- elapsed_seconds: float
- partial: true (only on partial completion)
- timeout: true (only when job-level timeout fired)
- max_files_reached: true (only when max_files cap fired)

### Large Result Paging

For results larger than ~2000 chars (configurable via Web UI `payload_preview_size_chars`), the polled job result is truncated and stored in PayloadCache. The response has these additional fields:

- `truncated: true` — set when the matches+errors JSON exceeded the preview cap
- `has_more: true` — synonym; set with `truncated`
- `cache_handle: "<uuid>"` — opaque handle for paged retrieval
- `total_size: <int>` — full payload byte size
- `matches_and_errors_preview: "<first 2000 chars of JSON>"` — quick preview
- `matches[]` and `evaluation_errors[]` — only the first 3 entries inline as quick scan

To fetch the full content: `GET /api/cache/{cache_handle}` (paged via `?page=N`).

When `truncated: false` (or absent), the full `matches[]` and `evaluation_errors[]` arrays are returned inline.

### Examples

**Explore AST structure for Java files calling prepareStatement:**
```json
{
  "repository_alias": "backend-global",
  "driver_regex": "prepareStatement",
  "evaluator_code": "return True",
  "search_target": "content",
  "include_patterns": ["*.java"],
  "max_files": 3,
  "max_debug_nodes": 30
}
```

**Explore first 2 Python test files to understand AST shape:**
```json
{
  "repository_alias": "backend-global",
  "driver_regex": "test_.*\\.py$",
  "evaluator_code": "return True",
  "search_target": "filename",
  "max_files": 2,
  "max_debug_nodes": 50
}
```
