---
name: xray_search
category: search
required_permission: query_repos
tl_dr: Two-phase AST-aware search — regex driver narrows candidates, Python evaluator inspects each AST node.
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
      description: 'Python expression evaluated in a sandboxed subprocess against each AST match position. Must return a bool. For search_target=content, node is the deepest AST node enclosing the regex match position; for search_target=filename, node equals root. Available names: node, root, source, lang, file_path.'
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
    max_files:
      type: integer
      description: 'Maximum number of candidate files to evaluate. When the cap is hit the result includes partial=true and max_files_reached=true. Use a small value (e.g. 5) to test your evaluator before running the full search. Must be >= 1 when provided.'
      minimum: 1
  required:
    - repository_alias
    - driver_regex
    - evaluator_code
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

Precision two-phase AST-aware code search. Returns {job_id} immediately; poll GET /api/jobs/{job_id} for results.

PHASE 1 (driver): regex driver narrows the file set — only files whose content (or path) matches driver_regex are passed to Phase 2. (Note: Phase 1 currently uses an inline sync regex driver; story #978 will migrate to RegexSearchService for ripgrep performance.)

PHASE 2 (evaluator): for each candidate file, the AST is parsed with tree-sitter and your Python evaluator_code runs in a sandboxed subprocess with access to: node (root XRayNode), root (root XRayNode), source (full file text), lang (language string), file_path (absolute path). Return True to include the file in matches.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str | yes | -- | Global repository alias, e.g. "myrepo-global". Use list_global_repos to see available repositories. |
| driver_regex | str | yes | -- | Regular expression applied in Phase 1 to file content or file paths to identify candidate files. Phase 1 match collection is capped at 100,000 regex hits across the repository. For very dense patterns on large repos, narrow the candidate set with include_patterns/exclude_patterns to avoid silent truncation. |
| evaluator_code | str | yes | -- | Python expression evaluated in a sandboxed subprocess against each AST match position. Must return a bool. For search_target=content, node is the deepest AST node enclosing the regex match position; for search_target=filename, node equals root. |
| search_target | "content" or "filename" | yes | -- | What the driver_regex applies to: "content" matches against file text (evaluator called once per match position with the enclosing AST node); "filename" matches against relative file paths (evaluator called once per file with node==root). |
| include_patterns | list[str] | no | [] | Glob patterns for files to include (e.g. ["*.java", "*.kt"]). Empty list means include all. |
| exclude_patterns | list[str] | no | [] | Glob patterns for files to exclude (e.g. ["*/test/*"]). Empty list means exclude none. |
| timeout_seconds | int | no | 120 | Per-job wall-clock timeout in seconds. Range: 10..600. Defaults to server config xray_timeout_seconds. |
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

## Polled Result Shape

After polling GET /api/jobs/{job_id} to COMPLETED status, result contains:

- matches[]: file_path, line_number, code_snippet, language, evaluator_decision
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

## Iterating on Your Evaluator

Iterating on an evaluator expression is a fundamental part of using xray_search. The recommended workflow:

1. Start with `max_files: 5` to test the evaluator on a small subset of candidate files. This prevents long waits during development and quickly reveals attribute mistakes.
2. Use `xray_explore` first to discover the AST shape produced by tree-sitter for the language you are searching. The `ast_debug` field shows you what fields and child types are available on each node, so your evaluator can reference them correctly.
3. After each run, read `evaluation_errors` carefully. AttributeError entries tell you that your evaluator referenced a node attribute that does not exist for this node type — common during early iterations. EvaluatorTimeout entries indicate your evaluator is too slow or has an infinite loop.
4. Once your evaluator runs cleanly on `max_files: 5` (no AttributeError, no EvaluatorTimeout), remove the `max_files` cap and run the full search.

## Examples

**Find all Java files that call prepareStatement:**
```json
{
  "repository_alias": "backend-global",
  "driver_regex": "prepareStatement",
  "evaluator_code": "return any(n.type == 'method_invocation' for n in root.named_children)",
  "search_target": "content",
  "include_patterns": ["*.java"]
}
```

**Test evaluator on 5 files before full search:**
```json
{
  "repository_alias": "backend-global",
  "driver_regex": "prepareStatement",
  "evaluator_code": "return True",
  "search_target": "content",
  "max_files": 5
}
```

**Find Python test files by path pattern (filename target):**
```json
{
  "repository_alias": "backend-global",
  "driver_regex": "test_.*\\.py$",
  "evaluator_code": "return True",
  "search_target": "filename"
}
```

**Search with include and exclude patterns (source files only, skip vendored code):**
```json
{
  "repository_alias": "backend-global",
  "driver_regex": "TODO|FIXME",
  "evaluator_code": "return True",
  "search_target": "content",
  "include_patterns": ["*.py", "*.java", "*.ts"],
  "exclude_patterns": ["*/vendor/*", "*/node_modules/*", "*/test/*"]
}
```

### Example: detect SQL injection via per-position node inspection

Find every `prepareStatement(...)` call that is NOT inside a try-with-resources statement (Java).

```json
{
  "repository_alias": "myapp-global",
  "driver_regex": "prepareStatement",
  "search_target": "content",
  "evaluator_code": "return node.type == 'method_invocation' and not node.is_descendant_of('try_with_resources_statement')",
  "include_patterns": ["*.java"]
}
```

Here `node` is the AST node enclosing each `prepareStatement` match — typically the `method_invocation` itself. We check both that the node IS an invocation (regex could have matched in a comment) AND that its ancestor chain does not include a try-with-resources block.

## Related

- See `xray_explore` for verbose AST debug output to help craft evaluator expressions.
- `xray_explore` runs the same two-phase pipeline but adds an `ast_debug` field to every match, showing the complete tree-sitter AST node structure. Use it before writing your evaluator_code.
