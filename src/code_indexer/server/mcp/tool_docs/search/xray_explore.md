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
    await_seconds:
      type: integer
      description: 'Optional server-side polling window in seconds. When 0 (default), the tool returns {job_id} immediately. When > 0, the server polls the background job for up to await_seconds seconds and returns the inline result if the job completes in time; otherwise falls back to {job_id}. Range: 0..30. Error code await_seconds_invalid if out of range or wrong type.'
      minimum: 0
      maximum: 30
      default: 0
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
| await_seconds | int | no | 0 | Server-side polling window in seconds. When 0, returns {job_id} immediately. When > 0, the server polls for up to await_seconds seconds and returns the inline result if the job completes; otherwise falls back to {job_id}. Range: 0..30. Error code: await_seconds_invalid. |

## Evaluator API

The evaluator code runs inside a sandboxed subprocess and receives these names:

- `node` — for `search_target='content'`, the deepest XRayNode whose [start_byte, end_byte) contains the Phase 1 regex match position (the "closest enclosing ancestor" at the match site). For `search_target='filename'`, this is the file root (same as `root`). Use `node` to inspect the specific construct at the match; use `root` to traverse the entire file tree.
- `root` — the file's root XRayNode (tree-sitter parse root)
- `source` — the full file content as a UTF-8 string
- `lang` — one of: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css
- `file_path` — absolute path of the file being evaluated

For `search_target='content'`: the evaluator is called once per regex match position; `node` is the deepest AST node enclosing that position. For `search_target='filename'`: the evaluator is called once per matching file with `node == root` (no per-position semantics — there is no in-content match position to enclose).

### XRayNode reference

The `node` argument passed to your evaluator code is an `XRayNode` instance. The full public surface:

| Name | Type | Description |
|------|------|-------------|
| `node.type` | str | tree-sitter node type, e.g. `'function_definition'`, `'call'`, `'if_statement'` |
| `node.parent` | XRayNode \| None | parent node; None for the file root |
| `node.children` | list[XRayNode] | all child nodes including anonymous ones (punctuation, keywords) |
| `node.named_children` | list[XRayNode] | only named children — usually what you want |
| `node.start_byte` | int | byte offset where the node starts |
| `node.end_byte` | int | byte offset where the node ends |
| `node.start_point` | tuple[int, int] | (row, column), zero-indexed |
| `node.end_point` | tuple[int, int] | (row, column), zero-indexed |
| `node.text` | str | raw source text — equivalent to `source[node.start_byte:node.end_byte]` |
| `node.is_descendant_of(type_name)` | bool | true if any ancestor matches `type_name` |
| `node.descendants_of_type(type_name)` | list[XRayNode] | DFS pre-order; all descendants whose type matches `type_name` (excludes self) |
| `node.count_descendants_of_type(type_name)` | int | fast count without materialising a list — use this when you only need a count |
| `node.enclosing(type_name)` | XRayNode \| None | walks UP parent chain (inclusive of self) and returns first ancestor matching `type_name` |

For a "common patterns cookbook" of 8 worked evaluator examples (filter to function bodies, exclude comments, count elif clauses, deep-nesting detection, etc.) and a cross-language node type table covering the 10 mandatory languages (Python / Java / TypeScript / JavaScript / Go / Kotlin / C# / etc.), see the corresponding sections in `xray_search.md`. The same evaluator API and `XRayNode` surface apply to both tools.

Whitelisted Python AST node types (all others are rejected before any subprocess is spawned):

- Expression core: `Call, Name, Attribute, Constant, Subscript, Compare, BoolOp, UnaryOp, List, Tuple, Dict, Return, Expr`
- Local binding: `Assign` (e.g. `x = node.named_children`), `AugAssign` (e.g. `count += 1`)
- Comprehensions and ternary: `comprehension, GeneratorExp, ListComp, SetComp, DictComp, IfExp`
- Abstract operator base classes (matched via isinstance against concrete subclasses): `boolop, cmpop, unaryop, expr_context, operator`
- Module/Load markers: `Module, Load`

Top-level `for` and `while` statements remain BANNED — use a comprehension (`ListComp` / `GeneratorExp` / `SetComp` / `DictComp`) or a generator-driven `any()` / `all()` / `sum()` instead. `class`, `def`, `import`, `lambda`, `try`, `with`, `global`, `nonlocal` are also banned.

As of v10.3.0 the sandbox accepts assignments and comprehensions, so these idiomatic patterns work:

- `[c for c in node.named_children if c.type == 'X']`
- `any(c.type == 'X' for c in node.named_children)`
- `count = node.count_descendants_of_type('X')\nreturn count >= 5`
- `result = True if cond else False`
- `total = 0\ntotal += len(node.named_children)\nreturn total > 0`

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
1. AST whitelist — rejects top-level `Import`, `For`, `While`, `With`, `Try`, `FunctionDef`, `ClassDef`, `Lambda`, and other unsupported nodes before any subprocess is spawned. Comprehension nodes (`comprehension`, `GeneratorExp`, `ListComp`, `SetComp`, `DictComp`), local assignments (`Assign`, `AugAssign`), and ternaries (`IfExp`) are accepted — see the whitelist above.
2. Stripped builtins — exec() environment removes getattr, setattr, open, eval, exec, compile, __import__.
3. 5-second hard timeout with SIGTERM + SIGKILL escalation.

### Polled Result Shape

After polling GET /api/jobs/{job_id} to COMPLETED status, result contains:

- matches[]: file_path, line_number, code_snippet, language, evaluator_decision, matched_node, ast_debug
  - As of v10.2.0, `line_number` and `code_snippet` are populated from Phase 1 ripgrep match positions (`RegexSearchService`) for `search_target='content'`. For `search_target='filename'` searches, `line_number` is `null` and `code_snippet` is `null` because the regex matched a path, not in-content text — the evaluator was called once per file with `node == root`.
  - `matched_node` — compact description of the deepest enclosing AST node passed to the evaluator (the same node that `node` refers to in evaluator_code). Present on every match entry when xray_explore is used. Fields: `type` (str, tree-sitter node type), `start_byte` (int), `end_byte` (int), `start_point` ([row, col] list), `end_point` ([row, col] list). Distinct from `ast_debug` which is always rooted at the file root; `matched_node` describes only the evaluator's input node.
  - `ast_debug` — full BFS-serialised AST tree rooted at the parse root (see ast_debug Payload section above).
- evaluation_errors[]: list of per-match evaluator failures. Each entry has file_path, line_number, error_type, error_message. error_type is one of: AttributeError (evaluator referenced a node attribute that does not exist for this node type), EvaluatorTimeout (the 5s sandbox timer fired), EvaluatorCrash (subprocess exited with non-zero code), UnsupportedLanguage, NonBoolReturn. evaluation_errors does NOT cause job failure — status remains COMPLETED.
- files_processed: int — number of candidate files evaluated
- files_total: int — total candidate files found by driver
- elapsed_seconds: float
- partial: true (only on partial completion)
- timeout: true (only when job-level timeout fired)
- max_files_reached: true (only when max_files cap fired)

### evaluation_errors[] payload examples

xray_explore reuses the same `XRaySearchEngine.run()` pipeline as xray_search, so the `evaluation_errors[]` shape is identical. Each error_type carries a distinct error_message shape (sourced from `src/code_indexer/xray/search_engine.py::_evaluate_file` and `src/code_indexer/xray/sandbox.py::EvalResult`):

**EvaluatorTimeout** — sandbox 5s wall-clock budget exceeded; subprocess received SIGTERM (and SIGKILL after a 1.0s grace period if still alive):

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/server/services/very_large_module.py",
  "line_number": 142,
  "error_type": "EvaluatorTimeout",
  "error_message": "evaluator exceeded 5s sandbox limit"
}
```

**EvaluatorCrash** — subprocess died before returning a value. The `error_message` carries the failure detail in one of three forms:

- `exitcode=<N>` — the subprocess exited with a non-zero status (e.g. `exitcode=139` indicates a SIGSEGV from a C-level fault inside tree-sitter or another extension).
- `no_pipe_data` — the subprocess died without sending any data over the pipe and returned an `exitcode` of None (typically a forked-process accounting race; treat as "subprocess died silently").
- `__exception__:<TypeName>:<message>` — the subprocess raised a Python exception inside `_run_evaluator` and serialised it before exiting cleanly. Common when stripped builtins are referenced (e.g. `getattr` raises `NameError`) or when an attribute lookup on the AST node fails.

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/cli.py",
  "line_number": 87,
  "error_type": "EvaluatorCrash",
  "error_message": "exitcode=139"
}
```

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/handlers/edge_case.py",
  "line_number": 23,
  "error_type": "EvaluatorCrash",
  "error_message": "__exception__:NameError:name 'getattr' is not defined"
}
```

**NonBoolReturn** — subprocess exited cleanly but the evaluator returned something other than `bool`. The `error_message` is exactly the type name from the subprocess (`type(value).__name__`):

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/server/handler.py",
  "line_number": 56,
  "error_type": "NonBoolReturn",
  "error_message": "list"
}
```

Other observed `error_message` values for this error_type: `"int"`, `"str"`, `"NoneType"`, `"dict"`. Wrap the evaluator return expression in `bool(...)` or use an explicit `return True/False` to fix.

**UnsupportedLanguage** — Phase 1 selected a candidate file whose extension has no tree-sitter grammar registered. The 10 mandatory languages are: java, kotlin, go, python, typescript, javascript, bash, csharp, html, css (terraform when `tree_sitter_hcl` is installed):

```json
{
  "file_path": "/srv/cidx/repo/docs/architecture.md",
  "line_number": 0,
  "error_type": "UnsupportedLanguage",
  "error_message": "No grammar for extension '.md'"
}
```

**Generic exception types** (e.g. `IOError`, `UnicodeDecodeError`, `OSError`) — emitted by the catch-all in `_evaluate_file` when the file itself cannot be read or parsed. The `error_type` is the Python exception class name and `error_message` is `str(exc)`.

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/server/symlinked_file.py",
  "line_number": 0,
  "error_type": "PermissionError",
  "error_message": "[Errno 13] Permission denied: '/srv/cidx/repo/src/code_indexer/server/symlinked_file.py'"
}
```

Note: `validation_failed` (sandbox AST whitelist rejection) does NOT appear in `evaluation_errors[]`. The handler validates `evaluator_code` synchronously BEFORE submitting the background job and returns a sync error response instead:

```json
{
  "error": "xray_evaluator_validation_failed",
  "message": "Lambda not in allowed nodes"
}
```

Other validation rejection messages include `"Import not in allowed nodes"`, `"For not in allowed nodes"` (top-level `for` statements — use a comprehension or `any()`/`all()` over a generator instead; the `comprehension`, `GeneratorExp`, `ListComp`, `SetComp`, and `DictComp` nodes ARE on the whitelist), `"Lambda not in allowed nodes"`, `"FunctionDef not in allowed nodes"`, `"While not in allowed nodes"`, `"Try not in allowed nodes"`, `"Attribute access to '__class__' blocked (sandbox escape vector)"`, `"Subscript access to '__import__' blocked (sandbox escape vector)"`, and `"syntax_error: <SyntaxError repr>"`.

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
