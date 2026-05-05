---
name: xray_explore
category: search
required_permission: query_repos
tl_dr: Debug-mode X-Ray search — same two-phase driver+evaluator as xray_search but enriches every match with a serialised AST tree so you can understand the node structure tree-sitter produces for your code.
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
      description: 'Optional. Python code snippet evaluated ONCE per candidate file in a sandboxed subprocess. Receives globals: node (file root XRayNode), root (alias), source (file UTF-8 text), lang (language name), file_path (absolute path), match_positions (list of dicts: one per Phase 1 hit, each with line_number/column/line_content/byte_offset/context_before/context_after; empty list in filename mode). MUST return a dict with shape {"matches": [...], "value": <any>}. Defaults to a snippet that emits one match per Phase 1 hit (or a single file-level match in filename mode), accepting all candidate files for AST exploration without requiring the caller to write their own evaluator. Each match in the list is a dict requiring at minimum line_number; may carry any open keys.'
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
      description: 'Lines of context before/after each Phase 1 hit. Range 0..10. Surfaces in match_positions[].context_before/context_after. Default 0.'
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
      description: 'Optional server-side polling window in seconds. Accepts floats (e.g. 2.5). When 0 (default), returns {job_id} immediately. When > 0, the server polls the background job for up to await_seconds and returns the inline result if the job completes; otherwise falls back to {job_id}. Range 0.0..10.0 (lowered from 30 in v10.3.2 to keep server-side polling within threadpool capacity). Error code await_seconds_invalid if out of range or wrong type.'
      minimum: 0
      maximum: 10.0
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
      description: 'Multi-repo: per-alias resolution failures.'
    matches:
      type: array
      description: 'Inline match list when await_seconds resolves. Each entry has file_path, language, line_number, line_content, matched_node, ast_debug, plus any open keys returned by the evaluator.'
      items:
        type: object
    file_metadata:
      type: array
      description: 'Inline per-file value list when await_seconds resolves.'
      items:
        type: object
    error:
      type: string
      description: 'Error code when the request is rejected synchronously.'
    message:
      type: string
      description: 'Human-readable description of the error.'
---

Developer-facing exploration tool for understanding AST node structure.

Use this tool when writing or debugging an `evaluator_code` snippet for `xray_search`. It runs the same two-phase pipeline as `xray_search` but adds an `ast_debug` field to every match showing the complete tree-sitter AST rooted at the matched file's parse root, plus a `matched_node` summary of that root.

For production search workflows use `xray_search` instead — it is faster because it omits the AST serialisation overhead.

PHASE 1 (driver, regex): the `pattern` regex narrows the file set. For `search_target='content'`, RegexSearchService (ripgrep-backed) walks the repo and records every hit's line number, column, line content, and context lines. For `search_target='filename'`, an inline path walker matches relative file paths. Phase 1 honors `path`, `include_patterns`, `exclude_patterns`, `case_sensitive`, `multiline`, `pcre2`, and `context_lines`.

PHASE 2 (evaluator, AST): for each candidate file, tree-sitter parses the file once, then your `evaluator_code` runs ONCE in a sandboxed subprocess with the file root AST node and the full list of Phase 1 hits for that file. The evaluator returns a dict `{"matches": [...], "value": ...}`. The server enriches each match with `file_path`, `language`, `line_content` (when omitted), `matched_node`, and `ast_debug`.

Returns `{job_id}` (single repo) or `{job_ids, errors}` (multi-repo) immediately; poll `GET /api/jobs/{job_id}` for results, or set `await_seconds > 0` to inline-wait up to 10 seconds.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str OR list[str] | yes | -- | Single alias or array (or JSON-encoded array string) for omni multi-repo exploration. |
| pattern | str | yes | -- | Regular expression applied in Phase 1. Renamed from `driver_regex` in v10.3.x. |
| evaluator_code | str | no | (default acceptor) | Python code snippet evaluated ONCE per file. Returns `{"matches": [...], "value": <any>}`. Defaults to a snippet that emits one match per Phase 1 hit, accepting all candidate files for AST exploration. |
| search_target | "content" or "filename" | yes | -- | "content" — Phase 1 regex applies to file text; `match_positions` is populated. "filename" — Phase 1 regex applies to relative paths; `match_positions` is empty. |
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
| await_seconds | float | no | 0 | Server-side inline-wait window (0.0..10.0). |

## Evaluator API

The evaluator API for `xray_explore` is identical to `xray_search`. The evaluator runs ONCE per file (file-as-unit contract) and returns a dict `{"matches": [...], "value": ...}`. The server enriches each match additionally with `matched_node` and `ast_debug` for `xray_explore`.

### Globals exposed to your evaluator

| Name | Type | Semantics |
|------|------|-----------|
| `node` | `XRayNode` | The file's root XRayNode (tree-sitter parse tree root). Walk DOWN via `node.descendants_of_type(...)`. |
| `root` | `XRayNode` | Alias for `node`. Same object. |
| `source` | `str` | Full file content as a UTF-8 string. Equivalent to `node.text`. |
| `lang` | `str` | tree-sitter language name. One of: `java`, `kotlin`, `go`, `python`, `typescript`, `javascript`, `bash`, `csharp`, `html`, `css` (and `terraform` when `tree_sitter_hcl` is installed). |
| `file_path` | `str` | Absolute path of the file being evaluated. |
| `match_positions` | `list[dict]` | List of every Phase 1 regex hit in this file. Each entry: `{"line_number": int, "column": int, "line_content": str, "byte_offset": int, "context_before": list[str], "context_after": list[str]}`. EMPTY LIST in `search_target='filename'` mode. |

The legacy per-position globals `match_byte_offset`, `match_line_number`, `match_line_content` are still passed (always `None` under the file-as-unit contract) and SHOULD NOT be referenced by new evaluators.

### Return contract

The evaluator MUST return a dict with the shape `{"matches": [...], "value": <any or None>}`. See the `xray_search` doc "Return contract" section for the full specification.

### XRayNode reference

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
| `node.count_descendants_of_type(type_name)` | int | fast count without materialising a list |
| `node.enclosing(type_name)` | XRayNode \| None | walks UP parent chain (inclusive of self) and returns first ancestor matching `type_name` |

For a 15-pattern cookbook of worked evaluator examples (filter to function bodies via `byte_offset`, exclude comments, count elif clauses, deep-nesting detection, branch counts, returns inside `if`, calls without try/except, TODO/FIXME audit, missing return-type annotations, bare `except:`, classes with no docstring, etc.) and a cross-language node type table covering the 10 mandatory languages, see the corresponding sections in `xray_search.md`. The same evaluator API and `XRayNode` surface apply to both tools.

### Whitelisted node types

The sandbox accepts the following Python AST node types in evaluator code (everything else is rejected at validation time before any subprocess is spawned):

- Expression core: `Call, Name, Attribute, Constant, Subscript, Compare, BoolOp, UnaryOp, BinOp, List, Tuple, Dict, Return, Expr`
- Local binding: `Assign`, `AugAssign`
- Comprehensions and ternary: `comprehension, GeneratorExp, ListComp, SetComp, DictComp, IfExp`
- Statement-level control flow (v10.4.0): `If, For, While, Break, Continue, Pass`
- Structured exception handling (v10.4.0): `Try, ExceptHandler, Raise`
- Abstract operator base classes (matched via isinstance against concrete subclasses Add, Sub, Eq, And, Not, Load, Store, etc.): `boolop, cmpop, unaryop, expr_context, operator`
- Module/Load markers: `Module, Load`

> **Termination guarantee**: infinite loops and unbounded iteration in your evaluator do NOT cause validation rejection — they hit the subprocess hard timeout (HARD_TIMEOUT_SECONDS = 5.0 s, SIGTERM; SIGKILL_GRACE_SECONDS = 1.0 s grace) and surface as `EvaluatorTimeout` in `evaluation_errors[]`.

**Still banned**: `class`, `def`, `async def`, `lambda`, `import`, `from ... import`, `global`, `nonlocal`, `with`, `async with`, `async`, `await`, `yield`, `yield from`.

**Safe builtins** (available in the exec environment):
`len, str, int, bool, list, tuple, dict, min, max, sum, any, all, range, enumerate, zip, sorted, reversed, hasattr` plus exception types for `except` clauses: `Exception, ValueError, TypeError, RuntimeError, AttributeError, KeyError, IndexError, NameError, StopIteration`.

**Stripped builtins** (removed from the exec environment — referencing them raises NameError):
`getattr, setattr, delattr, __import__, eval, exec, open, compile`.

**Dunder attribute blocklist** (Attribute and Subscript access to these names is rejected at AST validation time as sandbox escape vectors):

- Class introspection: `__class__, __bases__, __base__, __mro__, __subclasses__`
- Constructor/lifecycle: `__init__, __init_subclass__, __new__`
- Module/globals access: `__globals__, __builtins__, __import__, __module__, __loader__, __spec__, __file__, __path__, __package__, __cached__`
- Object internals: `__dict__, __getattribute__, __setattr__, __delattr__`
- Reduce/pickling: `__reduce__, __reduce_ex__`
- Function attrs: `__call__, __code__, __closure__, __func__, __defaults__, __kwdefaults__, __annotations__`
- Naming: `__name__, __qualname__`
- Modern features: `__type_params__`
- Descriptor/metaclass: `__set_name__, __instancecheck__, __subclasscheck__, __prepare__`
- Memory: `__weakref__`

Any attribute name matching `__*__` is also blocked at Subscript level (e.g. `globals()['__import__']`).

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
- `type` — tree-sitter node type string (e.g. "method_invocation", "identifier")
- `start_byte` / `end_byte` — byte offsets into the source file
- `start_point` / `end_point` — [row, col] line/column positions (0-indexed)
- `text_preview` — first 80 characters of the node's source text (UTF-8)
- `child_count` — total direct children in the full tree (may exceed children list size when truncated)
- `children` — serialised child nodes; contains `{"type": "...truncated"}` when the `max_debug_nodes` cap is hit

### Evaluator Safety

The sandbox enforces three defence layers:
1. AST whitelist — rejects any node type outside `ALLOWED_NODES` (and any dunder Attribute/Subscript access) before any subprocess is spawned.
2. Stripped builtins — exec() environment removes `getattr, setattr, delattr, open, eval, exec, compile, __import__`.
3. 5-second hard timeout with SIGTERM + 1-second SIGKILL escalation enforced via `multiprocessing.Process` isolation.

### Polled Result Shape

After polling `GET /api/jobs/{job_id}` to COMPLETED status, `result` contains:

- `matches[]`: list of enriched match dicts. Every entry contains:
  - `file_path` (str, server-added)
  - `language` (str, server-added)
  - `line_number` (int, evaluator-supplied)
  - `line_content` (str, server-derived if evaluator omitted)
  - `matched_node` (server-added): compact description of the file root node — `{type, start_byte, end_byte, start_point, end_point}`. Distinct from the `node` evaluator global only in serialisation; both refer to the file root under the v10.4.0 contract.
  - `ast_debug` (server-added): full BFS-serialised AST tree rooted at the file root (see "ast_debug Payload" above).
  - any open keys the evaluator chose to include
- `file_metadata[]`: per-file `value` entries from the evaluator: `{file_path, value}`. Files whose evaluator returned `value=None` are NOT in this list.
- `evaluation_errors[]`: list of per-file failures. Each entry: `{file_path, line_number, error_type, error_message}`. `evaluation_errors` does NOT cause job failure — status remains COMPLETED.
- `files_processed` (int): number of candidate files evaluated.
- `files_total` (int): total candidate files found by Phase 1.
- `elapsed_seconds` (float)
- `partial: true` (only on partial completion)
- `timeout: true` (only when job-level timeout fired — takes precedence over `max_files_reached`)
- `max_files_reached: true` (only when the `max_results` cap fired before timeout)
- `warnings[]` (only when present): zero-match include_pattern hints

### evaluation_errors[] payload examples

`xray_explore` reuses the same `XRaySearchEngine.run()` pipeline as `xray_search`, so the `evaluation_errors[]` shape is identical:

**EvaluatorTimeout** — sandbox 5s wall-clock budget exceeded:

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/server/services/very_large_module.py",
  "line_number": 0,
  "error_type": "EvaluatorTimeout",
  "error_message": "evaluator exceeded 5s sandbox limit"
}
```

**EvaluatorCrash** — subprocess died before returning a value. The `error_message` carries the failure detail in one of three forms: `exitcode=<N>`, `no_pipe_data`, or `__exception__:<TypeName>:<message>`.

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/handlers/edge_case.py",
  "line_number": 0,
  "error_type": "EvaluatorCrash",
  "error_message": "__exception__:NameError:name 'getattr' is not defined"
}
```

**InvalidEvaluatorReturn** — subprocess exited cleanly but the return value did not match the v10.4.0 dict contract:

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/server/handler.py",
  "line_number": 0,
  "error_type": "InvalidEvaluatorReturn",
  "error_message": "Evaluator must return a dict {\"matches\": [...], \"value\": ...}, got 'bool'. Note: bool return (legacy contract) is no longer accepted."
}
```

**UnsupportedLanguage** — Phase 1 selected a candidate whose extension has no tree-sitter grammar:

```json
{
  "file_path": "/srv/cidx/repo/docs/architecture.md",
  "line_number": 0,
  "error_type": "UnsupportedLanguage",
  "error_message": "No grammar for extension '.md'"
}
```

**ValidationFailed** — appears in `evaluation_errors[]` only as a defensive fallback. The handler validates `evaluator_code` synchronously BEFORE submitting the job and returns a sync error response in the normal path:

```json
{
  "error": "xray_evaluator_validation_failed",
  "message": "'Lambda' is not allowed in evaluator code. Lambdas are not allowed. Inline the boolean expression directly, or assign with `=` to a local variable. Whitelisted nodes: Assign, AugAssign, ..."
}
```

Other validation rejection messages name the offending node type (e.g. `'Import' is not allowed in evaluator code.`, `'ClassDef' is not allowed in evaluator code.`, `'With' is not allowed in evaluator code.`) and include the full whitelist in the message body. Dunder access produces `Attribute access to '__class__' blocked (sandbox escape vector)` or `Subscript access to '__import__' blocked (sandbox escape vector)`.

**Generic exception types** (e.g. `IOError`, `UnicodeDecodeError`, `OSError`, `PermissionError`) — emitted by the catch-all in `_evaluate_file` when the file cannot be read or parsed. The `error_type` is the Python exception class name; `error_message` is `str(exc)`.

### Large Result Paging

`xray_explore` results are typically much larger than `xray_search` results because every match carries an `ast_debug` payload of up to `max_debug_nodes` AST nodes. Truncation kicks in earlier — keep `max_results` and `max_debug_nodes` small while iterating.

For results larger than ~2000 chars (configurable via Web UI `payload_preview_size_chars`), the polled job result is truncated and stored in PayloadCache:

- `truncated: true` — set when the matches+errors JSON exceeded the preview cap
- `has_more: true` — synonym; set with `truncated`
- `cache_handle: "<uuid>"` — opaque handle for paged retrieval
- `total_size: <int>` — full payload byte size
- `matches_and_errors_preview: "<first 2000 chars of JSON>"` — quick preview
- `matches[]` and `evaluation_errors[]` — only the first 3 entries inline

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

**Explore first 2 Python test files to understand AST shape** (filename target — emit one match per file):
```json
{
  "repository_alias": "backend-global",
  "pattern": "test_.*\\.py$",
  "evaluator_code": "return {\"matches\": [{\"line_number\": 1}], \"value\": None}",
  "search_target": "filename",
  "max_results": 2,
  "max_debug_nodes": 50
}
```

**Explore matches with custom evaluator metadata** (carry through `complexity` per match):
```json
{
  "repository_alias": "backend-global",
  "pattern": "def ",
  "evaluator_code": "funcs = node.descendants_of_type('function_definition')\nmatches = []\nfor f in funcs:\n    cx = f.count_descendants_of_type('if_statement') + f.count_descendants_of_type('elif_clause')\n    matches.append({'line_number': f.start_point[0] + 1, 'complexity': cx})\nreturn {'matches': matches, 'value': {'function_count': len(funcs)}}",
  "search_target": "content",
  "include_patterns": ["*.py"],
  "max_results": 5,
  "max_debug_nodes": 20
}
```

## Related

- See `xray_search` for the production search variant (no AST debug overhead).
- See `xray_dump_ast` for a synchronous single-file AST dump (no Phase 1 driver, no evaluator).
- See `cidx_fetch_cached_payload` to retrieve large truncated results by `cache_handle`.
