---
name: xray_search
category: search
required_permission: query_repos
tl_dr: Two-phase AST-aware search — regex driver narrows candidate files, Python evaluator inspects each file's AST and returns a list of matches with open-ended per-match metadata.
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
      description: 'Python code snippet evaluated ONCE per candidate file in a sandboxed subprocess. Receives globals: node (file root XRayNode), root (alias for node), source (file UTF-8 text), lang (language name), file_path (absolute path), match_positions (list of dicts: one per Phase 1 hit, each with line_number/column/line_content/byte_offset/context_before/context_after; empty list in filename mode). MUST return a dict with shape {"matches": [...], "value": <any>}. Each match in the list is a dict requiring at minimum line_number; may carry any open keys. Server enriches every match with file_path, language, and (if omitted) line_content derived from source. Per-file value is collected into the response file_metadata list.'
    search_target:
      type: string
      enum:
        - content
        - filename
      description: 'What pattern applies to: "content" matches file text (Phase 1 hits populate match_positions), "filename" matches relative file paths (match_positions is empty).'
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
      description: 'Lines of context before/after each Phase 1 hit. Range 0..10. Surfaces in match_positions[].context_before/context_after when search_target=content. Default 0.'
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
      description: 'Optional server-side polling window in seconds. Accepts floats (e.g. 2.5). When 0 (default), returns {job_id} immediately. When > 0, the server polls the background job for up to await_seconds and returns the inline result if the job completes; otherwise falls back to {job_id}. Range 0.0..10.0 (lowered from 30 in v10.3.2 to keep server-side polling within threadpool capacity). Error code await_seconds_invalid if out of range or wrong type.'
      minimum: 0
      maximum: 10.0
      default: 0
  required:
    - repository_alias
    - pattern
    - evaluator_code
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
      description: 'Inline match list when await_seconds resolves. Each entry has file_path, language, line_number, line_content, plus any open keys returned by the evaluator (and matched_node/ast_debug for xray_explore).'
      items:
        type: object
    file_metadata:
      type: array
      description: 'Inline per-file value list when await_seconds resolves. One entry per file whose evaluator returned a non-None value: {file_path, value}.'
      items:
        type: object
    error:
      type: string
      description: 'Error code when the request is rejected synchronously.'
    message:
      type: string
      description: 'Human-readable description of the error.'
---

Two-phase AST-aware code search.

PHASE 1 (driver, regex): the `pattern` regex narrows the file set. For `search_target='content'`, RegexSearchService (ripgrep-backed) walks the repo and records every hit's line number, column, line content, and context lines. For `search_target='filename'`, an inline path walker matches relative file paths. Phase 1 honors `path`, `include_patterns`, `exclude_patterns`, `case_sensitive`, `multiline`, `pcre2`, and `context_lines`.

PHASE 2 (evaluator, AST): for each candidate file, tree-sitter parses the file once, then your `evaluator_code` runs ONCE in a sandboxed subprocess with the file root AST node and the full list of Phase 1 hits for that file. The evaluator returns a dict carrying its own per-match list and an optional per-file value. The server enriches each match with `file_path`, `language`, and (when omitted) `line_content`.

Returns `{job_id}` (single repo) or `{job_ids, errors}` (multi-repo) immediately; poll `GET /api/jobs/{job_id}` for results, or set `await_seconds > 0` to inline-wait up to 10 seconds.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str OR list[str] | yes | -- | Single alias for single-repo search. Array (or JSON-encoded array string) for omni multi-repo search. Multi-repo returns one job id per resolved alias plus an `errors[]` list for unresolved aliases. |
| pattern | str | yes | -- | Regular expression applied in Phase 1. Renamed from `driver_regex` in v10.3.x. |
| evaluator_code | str | yes | -- | Python code snippet evaluated ONCE per file. Returns `{"matches": [...], "value": <any>}`. See "Evaluator API" below. |
| search_target | "content" or "filename" | yes | -- | "content" — Phase 1 regex applies to file text; `match_positions` is populated. "filename" — Phase 1 regex applies to relative paths; `match_positions` is empty. |
| include_patterns | list[str] | no | [] | Glob patterns for files to include. `*` matches a single path segment; use `**` for recursive matching. Empty means include all. |
| exclude_patterns | list[str] | no | [] | Glob patterns for files to exclude. Empty means exclude none. |
| path | str | no | null | Subdirectory restriction within the repo (relative). |
| case_sensitive | bool | no | true | Phase 1 content driver case sensitivity. |
| context_lines | int | no | 0 | Lines of context before/after each Phase 1 hit. Range 0..10. Surface in `match_positions[].context_before` / `context_after`. |
| multiline | bool | no | false | Multi-line regex matching in the content driver. |
| pcre2 | bool | no | false | PCRE2 engine for the content driver (lookahead/lookbehind). |
| timeout_seconds | int | no | 120 | Per-job wall-clock cap. Range 10..600. |
| max_results | int | no | null | Cap on candidate files evaluated. When hit: `partial=true`, `max_files_reached=true`. Renamed from `max_files` in v10.3.x. |
| await_seconds | float | no | 0 | Server-side inline-wait window. 0 = return job id immediately. Range 0.0..10.0. |

## Evaluator API

### File-as-unit contract (v10.4.0)

The evaluator runs **ONCE per candidate file**, not per Phase 1 hit. It receives the file root AST node plus the entire list of Phase 1 hits for the file, and returns a dict carrying its own list of matches and an optional per-file value.

```python
# Minimum viable evaluator
return {"matches": [], "value": None}
```

```python
# Echo every Phase 1 hit as a match (no AST filtering)
return {
    "matches": [{"line_number": p["line_number"]} for p in match_positions],
    "value": None,
}
```

### Globals exposed to your evaluator

| Name | Type | Semantics |
|------|------|-----------|
| `node` | `XRayNode` | The file's root XRayNode (tree-sitter parse tree root). Walk DOWN via `node.descendants_of_type(...)`. |
| `root` | `XRayNode` | Alias for `node`. Same object. |
| `source` | `str` | Full file content as a UTF-8 string. Equivalent to `node.text`. |
| `lang` | `str` | tree-sitter language name. One of: `java`, `kotlin`, `go`, `python`, `typescript`, `javascript`, `bash`, `csharp`, `html`, `css` (and `terraform` when `tree_sitter_hcl` is installed). |
| `file_path` | `str` | Absolute path of the file being evaluated. |
| `match_positions` | `list[dict]` | List of every Phase 1 regex hit in this file. Each entry: `{"line_number": int, "column": int, "line_content": str, "byte_offset": int, "context_before": list[str], "context_after": list[str]}`. EMPTY LIST in `search_target='filename'` mode. |

The legacy per-position globals `match_byte_offset`, `match_line_number`, `match_line_content` are still passed (always `None` under the file-as-unit contract) and SHOULD NOT be referenced by new evaluators. Use `match_positions` instead.

### Return contract

The evaluator MUST return a dict with the following shape:

```python
{
    "matches": [
        {"line_number": int, ...},   # required key per match: line_number
        ...
    ],
    "value": <anything or None>,     # optional per-file metadata
}
```

- `matches` (required): list of dicts. Each match dict requires `line_number: int`. May carry any open keys: `column`, `line_content`, `context_before`, `context_after`, plus arbitrary application-specific fields (e.g. `complexity_score`, `severity`, `enclosing_function`, `notes`).
- `value` (optional): an open-typed per-file payload. When non-None it is collected into the response `file_metadata[]` list as `{"file_path": ..., "value": <value>}`. Useful for whole-file metrics (line count, total complexity, list of imported modules).

### Server enrichment

The server adds the following to every match dict before returning it in the response `matches[]`:

- `file_path` (always added): absolute path of the file being evaluated. Overrides any value the evaluator put there.
- `language` (always added): tree-sitter language name.
- `line_content` (added only when the evaluator omitted it): derived from `source` using `line_number` (1-based). Empty string if `line_number` is out of range.

For `xray_explore` only, server additionally adds `matched_node` (compact description of the file root) and `ast_debug` (BFS-serialised AST tree).

### XRayNode reference

The full public surface of any `XRayNode` reachable via `node`, `descendants_of_type`, `children`, `named_children`, `parent`, or `enclosing(...)`:

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

### Whitelisted node types

The sandbox accepts the following Python AST node types in evaluator code (everything else is rejected at validation time before any subprocess is spawned):

- Expression core: `Call, Name, Attribute, Constant, Subscript, Compare, BoolOp, UnaryOp, BinOp, List, Tuple, Dict, Return, Expr`
- Local binding: `Assign` (e.g. `x = node.named_children`), `AugAssign` (e.g. `count += 1`)
- Comprehensions and ternary: `comprehension, GeneratorExp, ListComp, SetComp, DictComp, IfExp`
- Statement-level control flow (v10.4.0): `If` (statement-level if/elif/else), `For` (statement-level for-loop), `While` (statement-level while-loop), `Break`, `Continue`, `Pass`
- Structured exception handling (v10.4.0): `Try` (try/except/finally), `ExceptHandler` (except clauses, bare and typed), `Raise`
- Abstract operator base classes (matched via isinstance against concrete subclasses Add, Sub, Eq, And, Not, Load, Store, etc.): `boolop, cmpop, unaryop, expr_context, operator`
- Module/Load markers: `Module, Load`

> **Termination guarantee**: infinite loops and unbounded iteration in your evaluator do NOT cause validation rejection — they hit the subprocess hard timeout (HARD_TIMEOUT_SECONDS = 5.0 s, SIGTERM; SIGKILL_GRACE_SECONDS = 1.0 s grace) and surface as `EvaluatorTimeout` in `evaluation_errors[]`. The subprocess timeout is the authoritative termination boundary.

**Still banned** (rejected at validation time):

- Function/class/lambda definitions: `class`, `def`, `async def`, `lambda`
- Imports: `import`, `from ... import`
- Scope manipulation: `global`, `nonlocal`
- Resource managers: `with`, `async with`
- Async/await: `async`, `await`
- Generators: `yield`, `yield from`

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

### Cookbook: 15 worked patterns (v10.4.0 contract)

Each example is a complete `evaluator_code` value. All patterns return the v10.4.0 dict shape `{"matches": [...], "value": ...}`. The lifted bans on `if`/`for`/`while`/`try` make many of these clearer than the comprehension-only equivalents.

1. **Filter Phase 1 hits to those inside function bodies** (the most common ask):
   ```python
   funcs = node.descendants_of_type('function_definition')
   matches = []
   for pos in match_positions:
       offset = pos["byte_offset"]
       for f in funcs:
           if f.start_byte <= offset < f.end_byte:
               matches.append({
                   "line_number": pos["line_number"],
                   "enclosing_function": f.named_children[0].text if f.named_children else "<anonymous>",
               })
               break
   return {"matches": matches, "value": None}
   ```

2. **Exclude hits inside comments or string literals**:
   ```python
   comments = node.descendants_of_type('comment')
   strings  = node.descendants_of_type('string')
   excluded = comments + strings
   matches = []
   for pos in match_positions:
       offset = pos["byte_offset"]
       inside = False
       for n in excluded:
           if n.start_byte <= offset < n.end_byte:
               inside = True
               break
       if not inside:
           matches.append({"line_number": pos["line_number"]})
   return {"matches": matches, "value": None}
   ```

3. **Find hits inside a function with a specific name**:
   ```python
   target_name = 'public_method'
   funcs = node.descendants_of_type('function_definition')
   matches = []
   for pos in match_positions:
       offset = pos["byte_offset"]
       for f in funcs:
           if f.start_byte <= offset < f.end_byte:
               name_node = f.named_children[0] if f.named_children else None
               if name_node is not None and name_node.text == target_name:
                   matches.append({"line_number": pos["line_number"]})
               break
   return {"matches": matches, "value": None}
   ```

4. **Find functions with N+ elif clauses** (whole-file, ignores match_positions):
   ```python
   funcs = node.descendants_of_type('function_definition')
   matches = []
   for f in funcs:
       elif_count = f.count_descendants_of_type('elif_clause')
       if elif_count >= 5:
           matches.append({
               "line_number": f.start_point[0] + 1,
               "elif_count": elif_count,
           })
   return {"matches": matches, "value": {"function_count": len(funcs)}}
   ```

5. **SQL-shaped string literals at the Phase 1 hit**:
   ```python
   strings = node.descendants_of_type('string')
   matches = []
   for pos in match_positions:
       offset = pos["byte_offset"]
       for s in strings:
           if s.start_byte <= offset < s.end_byte and 'SELECT' in s.text.upper():
               matches.append({
                   "line_number": pos["line_number"],
                   "string_preview": s.text[:80],
               })
               break
   return {"matches": matches, "value": None}
   ```

6. **Functions with too many parameters** (whole-file metric):
   ```python
   funcs = node.descendants_of_type('function_definition')
   matches = []
   for f in funcs:
       params = f.descendants_of_type('parameter')
       if len(params) > 7:
           matches.append({
               "line_number": f.start_point[0] + 1,
               "param_count": len(params),
           })
   return {"matches": matches, "value": None}
   ```

7. **Deep nesting detection** (whole-file score):
   ```python
   ifs    = node.count_descendants_of_type('if_statement')
   fors   = node.count_descendants_of_type('for_statement')
   whiles = node.count_descendants_of_type('while_statement')
   total  = ifs + fors + whiles
   matches = []
   if total >= 10:
       matches.append({
           "line_number": 1,
           "ifs": ifs,
           "fors": fors,
           "whiles": whiles,
       })
   return {"matches": matches, "value": {"branching_total": total}}
   ```

8. **List comprehension presence anywhere in the file**:
   ```python
   count = node.count_descendants_of_type('list_comprehension')
   matches = []
   if count >= 1:
       matches.append({"line_number": 1, "list_comp_count": count})
   return {"matches": matches, "value": {"list_comp_count": count}}
   ```

9. **Functions with high cyclomatic complexity** (per-function score):
   ```python
   funcs = node.descendants_of_type('function_definition')
   matches = []
   for f in funcs:
       complexity = (
           f.count_descendants_of_type('if_statement') +
           f.count_descendants_of_type('elif_clause')
       )
       if complexity >= 8:
           matches.append({
               "line_number": f.start_point[0] + 1,
               "complexity": complexity,
           })
   return {"matches": matches, "value": None}
   ```

10. **Returns inside `if` statements** (one match per offending return):
    ```python
    returns = node.descendants_of_type('return_statement')
    matches = []
    for r in returns:
        if r.enclosing('if_statement') is not None:
            matches.append({"line_number": r.start_point[0] + 1})
    return {"matches": matches, "value": None}
    ```

11. **Calls without try/except guards** (audit risky operations) — uses `try`/`except` inside the evaluator itself for defensive node access:
    ```python
    calls = node.descendants_of_type('call')
    unsafe = []
    for c in calls:
        try:
            if c.enclosing('try_statement') is None:
                unsafe.append(c)
        except AttributeError:
            # Defensive: skip nodes that can't traverse parent chain
            continue
    matches = [{"line_number": c.start_point[0] + 1} for c in unsafe]
    return {"matches": matches, "value": {"unsafe_call_count": len(unsafe)}}
    ```

12. **TODO/FIXME comments** (one match per comment):
    ```python
    comments = node.descendants_of_type('comment')
    matches = []
    for c in comments:
        text = c.text
        if 'TODO' in text or 'FIXME' in text:
            matches.append({
                "line_number": c.start_point[0] + 1,
                "comment_text": text[:120],
            })
    return {"matches": matches, "value": {"todo_count": len(matches)}}
    ```

13. **Public functions missing return-type annotations (Python)**:
    ```python
    funcs = node.descendants_of_type('function_definition')
    matches = []
    for f in funcs:
        if not f.named_children:
            continue
        name_text = f.named_children[0].text
        if name_text.startswith('_'):
            continue
        has_return_type = False
        for c in f.named_children:
            if c.type == 'type':
                has_return_type = True
                break
        if not has_return_type:
            matches.append({
                "line_number": f.start_point[0] + 1,
                "function_name": name_text,
            })
    return {"matches": matches, "value": None}
    ```

14. **Bare `except:` clauses** (one match per bare except):
    ```python
    excepts = node.descendants_of_type('except_clause')
    matches = []
    for e in excepts:
        is_typed = False
        for c in e.named_children:
            if c.type in ('identifier', 'attribute', 'tuple'):
                is_typed = True
                break
        if not is_typed:
            matches.append({"line_number": e.start_point[0] + 1})
    return {"matches": matches, "value": None}
    ```

15. **Classes with no docstring** (one match per undocumented class):
    ```python
    classes = node.descendants_of_type('class_definition')
    matches = []
    for cls in classes:
        bodies = [c for c in cls.named_children if c.type == 'block']
        has_doc = False
        if bodies:
            block = bodies[0]
            if block.named_children:
                first = block.named_children[0]
                if first.type == 'expression_statement':
                    for c in first.named_children:
                        if c.type == 'string':
                            has_doc = True
                            break
        if not has_doc:
            class_name = cls.named_children[0].text if cls.named_children else "<unnamed>"
            matches.append({
                "line_number": cls.start_point[0] + 1,
                "class_name": class_name,
            })
    return {"matches": matches, "value": None}
    ```

### Common cross-language node type names

tree-sitter node type names differ between languages. Use this table as a starting reference for the 10 mandatory languages.

| Construct | Python | Java | TypeScript / JavaScript | Go | Kotlin | C# |
|-----------|--------|------|-------------------------|-----|--------|-----|
| Function definition | `function_definition` | `method_declaration` | `function_declaration` / `method_definition` | `function_declaration` | `function_declaration` | `method_declaration` |
| Function call | `call` | `method_invocation` | `call_expression` | `call_expression` | `call_expression` | `invocation_expression` |
| Class definition | `class_definition` | `class_declaration` | `class_declaration` | (no class — `type_declaration` for structs) | `class_declaration` | `class_declaration` |
| If statement | `if_statement` | `if_statement` | `if_statement` | `if_statement` | `if_expression` | `if_statement` |
| Else-if | `elif_clause` (Python-only) | `else if` chain inside `if_statement` | `else if` chain | `else if` chain | `else if` chain | `else if` chain |
| For loop | `for_statement` | `enhanced_for_statement` / `for_statement` | `for_statement` / `for_in_statement` | `for_statement` | `for_statement` | `for_statement` |
| Try block | `try_statement` | `try_statement` / `try_with_resources_statement` | `try_statement` | (no try — `defer`/`recover`) | `try_expression` | `try_statement` |
| Variable declaration | `assignment` (no separate decl) | `local_variable_declaration` | `lexical_declaration` (`let`/`const`) / `variable_declaration` (`var`) | `var_declaration` / `short_var_declaration` | `property_declaration` | `local_declaration_statement` |
| String literal | `string` | `string_literal` | `string` | `interpreted_string_literal` | `string_literal` | `string_literal` |
| Comment | `comment` | `line_comment` / `block_comment` | `comment` | `comment` | `line_comment` / `block_comment` | `comment` |

The fastest way to discover the exact type names for a construct is to use `xray_dump_ast` on a small example file in the language, or consult the tree-sitter grammar repository for that language.

## Polled Result Shape

After polling `GET /api/jobs/{job_id}` to COMPLETED status, `result` contains:

- `matches[]`: list of enriched match dicts. Every entry contains:
  - `file_path` (str, server-added) — absolute path
  - `language` (str, server-added) — tree-sitter language name
  - `line_number` (int, evaluator-supplied) — required field on every match dict the evaluator returns
  - `line_content` (str, server-derived if evaluator omitted) — raw text of `line_number` from `source`
  - any open keys the evaluator chose to include (e.g. `column`, `context_before`, `context_after`, `enclosing_function`, `complexity`, etc.)
- `file_metadata[]`: list of per-file value entries. One entry per file whose evaluator returned a non-None `value`. Shape: `{"file_path": str, "value": <any>}`. Files whose evaluator returned `value=None` (or omitted `value`) are NOT in this list.
- `evaluation_errors[]`: list of per-file failures. Each entry: `{file_path, line_number, error_type, error_message}`. `evaluation_errors` does NOT cause job failure — status remains COMPLETED.
- `files_processed` (int): number of candidate files evaluated.
- `files_total` (int): total candidate files found by Phase 1.
- `elapsed_seconds` (float)
- `partial: true` (only on partial completion)
- `timeout: true` (only when job-level timeout fired — takes precedence over `max_files_reached`)
- `max_files_reached: true` (only when the `max_results` cap fired before timeout)
- `warnings[]` (only when present): zero-match include_pattern hints

### evaluation_errors[] payload examples

Each `error_type` carries a distinct `error_message` shape:

**EvaluatorTimeout** — sandbox 5s wall-clock budget exceeded; subprocess received SIGTERM (and SIGKILL after a 1.0s grace if still alive):

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/server/services/very_large_module.py",
  "line_number": 0,
  "error_type": "EvaluatorTimeout",
  "error_message": "evaluator exceeded 5s sandbox limit"
}
```

**EvaluatorCrash** — subprocess died before returning a value. The `error_message` carries the failure detail in one of three forms:

- `exitcode=<N>` — non-zero status (e.g. `exitcode=139` indicates SIGSEGV)
- `no_pipe_data` — subprocess died without sending any data
- `__exception__:<TypeName>:<message>` — subprocess raised a Python exception (e.g. NameError when a stripped builtin is referenced, AttributeError on a missing node attribute)

```json
{
  "file_path": "/srv/cidx/repo/src/code_indexer/cli.py",
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

Other `InvalidEvaluatorReturn` messages: `"Evaluator dict missing required 'matches' key. Return: {\"matches\": [...], \"value\": ...}"`, `"'matches' must be a list, got 'dict'"`.

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

**Generic exception types** (e.g. `IOError`, `UnicodeDecodeError`, `OSError`) — emitted by the catch-all in `_evaluate_file` when the file cannot be read or parsed. The `error_type` is the Python exception class name; `error_message` is `str(exc)`.

### Large Result Paging

For results larger than ~2000 chars (configurable via Web UI `payload_preview_size_chars`), the polled job result is truncated and stored in PayloadCache. Additional fields:

- `truncated: true` — set when the matches+errors JSON exceeded the preview cap
- `has_more: true` — synonym; set with `truncated`
- `cache_handle: "<uuid>"` — opaque handle for paged retrieval
- `total_size: <int>` — full payload byte size
- `matches_and_errors_preview: "<first 2000 chars of JSON>"` — quick preview
- `matches[]` and `evaluation_errors[]` — only the first 3 entries inline

To fetch the full content: `GET /api/cache/{cache_handle}` (paged via `?page=N`), or use the discoverable `cidx_fetch_cached_payload` MCP tool.

When `truncated: false` (or absent), the full `matches[]` and `evaluation_errors[]` arrays are returned inline.

## Iterating on Your Evaluator

1. Start with `max_results: 5` to test the evaluator on a small subset of candidate files. This prevents long waits during development and quickly reveals attribute mistakes.
2. Use `xray_explore` first to discover the AST shape produced by tree-sitter for the language. The `ast_debug` field shows the available fields and child types so the evaluator can reference them correctly.
3. After each run, read `evaluation_errors` carefully:
   - `__exception__:AttributeError:...` — the evaluator referenced a node attribute that does not exist. The error message includes a `Did you mean: ...?` hint when a similar attribute is in the public XRayNode surface.
   - `EvaluatorTimeout` — the evaluator is too slow or has an unbounded loop. The 5s subprocess timeout fired.
   - `InvalidEvaluatorReturn` — the evaluator returned the wrong shape. Confirm the return is a dict with `matches: [...]`.
4. Once the evaluator runs cleanly on `max_results: 5`, remove the cap and run the full search.

## Examples

**Echo every prepareStatement Phase 1 hit as a match (no AST filtering)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "prepareStatement",
  "evaluator_code": "return {\"matches\": [{\"line_number\": p[\"line_number\"]} for p in match_positions], \"value\": None}",
  "search_target": "content",
  "include_patterns": ["*.java"]
}
```

**Test evaluator on 5 files before full search**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "prepareStatement",
  "evaluator_code": "return {\"matches\": [{\"line_number\": p[\"line_number\"]} for p in match_positions], \"value\": None}",
  "search_target": "content",
  "max_results": 5
}
```

**Find Python test files by path pattern (filename target — match_positions is empty, so emit one match for the file)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "test_.*\\.py$",
  "evaluator_code": "return {\"matches\": [{\"line_number\": 1}], \"value\": None}",
  "search_target": "filename"
}
```

**Search with include and exclude patterns (source files only, skip vendored code)**:
```json
{
  "repository_alias": "backend-global",
  "pattern": "TODO|FIXME",
  "evaluator_code": "comments = node.descendants_of_type('comment')\nmatches = []\nfor c in comments:\n    if 'TODO' in c.text or 'FIXME' in c.text:\n        matches.append({'line_number': c.start_point[0] + 1, 'comment_text': c.text[:120]})\nreturn {'matches': matches, 'value': {'todo_count': len(matches)}}",
  "search_target": "content",
  "include_patterns": ["*.py", "*.java", "*.ts"],
  "exclude_patterns": ["*/vendor/*", "*/node_modules/*", "*/test/*"]
}
```

**Multi-repo (omni) search across two repos** — returns `{job_ids: [...], errors: [...]}`:
```json
{
  "repository_alias": ["backend-global", "frontend-global"],
  "pattern": "TODO",
  "evaluator_code": "return {\"matches\": [{\"line_number\": p[\"line_number\"]} for p in match_positions], \"value\": None}",
  "search_target": "content"
}
```

### Example: detect SQL injection via per-file inspection (v10.4.0 contract)

Find every `prepareStatement(...)` call that is NOT inside a try-with-resources statement (Java). Walk DOWN to find method invocations, scope each invocation to the Phase 1 hits via `byte_offset`, then verify the enclosing try-with-resources is absent.

```json
{
  "repository_alias": "myapp-global",
  "pattern": "prepareStatement",
  "search_target": "content",
  "evaluator_code": "invs = node.descendants_of_type('method_invocation')\nmatches = []\nfor pos in match_positions:\n    offset = pos['byte_offset']\n    for inv in invs:\n        if inv.start_byte <= offset < inv.end_byte:\n            if inv.enclosing('try_with_resources_statement') is None:\n                matches.append({'line_number': pos['line_number'], 'risk': 'no_try_with_resources'})\n            break\nreturn {'matches': matches, 'value': None}",
  "include_patterns": ["*.java"]
}
```

The evaluator iterates `match_positions` (every Phase 1 hit), finds the enclosing `method_invocation` for each offset, and filters out invocations that have a `try_with_resources_statement` ancestor — surfacing only unsafe statements. The `risk` field is an open per-match key the server preserves intact.

## Related

- See `xray_explore` for verbose AST debug output to help craft evaluator code.
- `xray_explore` runs the same two-phase pipeline but adds an `ast_debug` field to every match, showing the complete tree-sitter AST node structure. Use it before writing your `evaluator_code`.
- See `cidx_fetch_cached_payload` to retrieve large truncated results by `cache_handle`.
