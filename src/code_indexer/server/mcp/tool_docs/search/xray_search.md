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
      description: 'Python expression evaluated in a sandboxed subprocess against each AST match position. Must return a bool. As of v10.3.2 the evaluator always receives node=root (the file parse tree) in BOTH content and filename modes — walk DOWN via node.descendants_of_type(...) to find specific constructs. Phase 1 regex match position is exposed separately via match_byte_offset / match_line_number / match_line_content (None in filename mode). Available names: node, root, source, lang, file_path, match_byte_offset, match_line_number, match_line_content.'
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
    await_seconds:
      type: number
      description: 'Optional server-side polling window in seconds. Accepts floats (e.g. 2.5). When 0 (default), the tool returns {job_id} immediately. When > 0, the server polls the background job for up to await_seconds seconds and returns the inline result if the job completes in time; otherwise falls back to {job_id}. Range: 0.0..10.0 (lowered from 30 in v10.3.2 to keep server-side polling within the threadpool capacity cap and avoid starving other tools). Error code await_seconds_invalid if out of range or wrong type.'
      minimum: 0
      maximum: 10.0
      default: 0
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

PHASE 2 (evaluator): for each Phase 1 match position, the AST is parsed with tree-sitter and your Python evaluator_code runs in a sandboxed subprocess. As of v10.3.2 the evaluator always receives `node = root` (the file parse tree) in BOTH content and filename modes — walk DOWN via `node.descendants_of_type(...)` to find specific constructs. The Phase 1 regex match position is exposed separately via `match_byte_offset`, `match_line_number`, and `match_line_content` (None in filename mode). Return True to include the match in results.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| repository_alias | str | yes | -- | Global repository alias, e.g. "myrepo-global". Use list_global_repos to see available repositories. |
| driver_regex | str | yes | -- | Regular expression applied in Phase 1 to file content or file paths to identify candidate files. Phase 1 match collection is capped at 100,000 regex hits across the repository. For very dense patterns on large repos, narrow the candidate set with include_patterns/exclude_patterns to avoid silent truncation. |
| evaluator_code | str | yes | -- | Python expression evaluated in a sandboxed subprocess against each AST match position. Must return a bool. As of v10.3.2 `node` is ALWAYS the file root in both content and filename modes — walk DOWN via `node.descendants_of_type(...)` to inspect specific constructs. Phase 1 regex match position is exposed separately via `match_byte_offset` / `match_line_number` / `match_line_content` (None in filename mode). |
| search_target | "content" or "filename" | yes | -- | What the driver_regex applies to: "content" matches against file text (evaluator called once per Phase 1 match position with `node==root` plus `match_byte_offset`/`match_line_number`/`match_line_content` populated); "filename" matches against relative file paths (evaluator called once per file with `node==root` and the three `match_*` metadata fields set to None). |
| include_patterns | list[str] | no | [] | Glob patterns for files to include (e.g. ["*.java", "*.kt"]). Empty list means include all. |
| exclude_patterns | list[str] | no | [] | Glob patterns for files to exclude (e.g. ["*/test/*"]). Empty list means exclude none. |
| timeout_seconds | int | no | 120 | Per-job wall-clock timeout in seconds. Range: 10..600. Defaults to server config xray_timeout_seconds. |
| max_files | int | no | null | Maximum number of candidate files to evaluate. When the cap is hit the result includes partial=true and max_files_reached=true. Use max_files: 5 to test your evaluator before running the full search. |
| await_seconds | float | no | 0 | Server-side polling window in seconds. Accepts floats (e.g. 2.5). When 0, returns {job_id} immediately. When > 0, the server polls for up to await_seconds seconds and returns the inline result if the job completes; otherwise falls back to {job_id}. Range: 0.0..10.0 (lowered from 30 in v10.3.2 to keep server-side polling within the threadpool capacity cap and avoid starving other tools). Error code: await_seconds_invalid. |

## Evaluator API

### Globals exposed to your evaluator

As of v10.3.2 the sandbox exposes 8 globals to your `evaluator_code` (was 5). The mental model changed: `node` is now ALWAYS the file root, and the Phase 1 regex match position is exposed as separate metadata. Walk DOWN from `node` via `descendants_of_type(...)` to find specific constructs; use `match_byte_offset` to know where the Phase 1 regex matched if you need to scope your descendants to the hit.

| Name | Type | Semantics |
|------|------|-----------|
| `node` | `XRayNode` | The file's root XRayNode (tree-sitter parse root). ALWAYS the root in both `content` and `filename` modes — v10.3.2 contract change. |
| `root` | `XRayNode` | Alias for `node`. Kept for backward compatibility and clarity when callers want the explicit "I want the file root" name. |
| `source` | `str` | Full file content as a UTF-8 string. Equivalent to `node.text`. |
| `lang` | `str` | tree-sitter language name. One of: `java`, `kotlin`, `go`, `python`, `typescript`, `javascript`, `bash`, `csharp`, `html`, `css` (and `terraform` when `tree_sitter_hcl` is installed). |
| `file_path` | `str` | Absolute path of the file being evaluated. |
| `match_byte_offset` | `int \| None` | NEW in v10.3.2 — Phase 1 regex match byte offset into `source`. `None` in `filename` mode (the regex matched a path, not file content). |
| `match_line_number` | `int \| None` | NEW in v10.3.2 — Phase 1 regex match 1-indexed line number. `None` in `filename` mode. |
| `match_line_content` | `str \| None` | NEW in v10.3.2 — raw text of the Phase 1 regex match line (no trailing newline). `None` in `filename` mode. |

For `search_target='content'`: the evaluator is called once per Phase 1 regex match position. `node` is the file root; the three `match_*` fields point at where the regex hit. To scope your inspection to the hit, walk down with `descendants_of_type` and filter by the byte range, e.g. `[f for f in node.descendants_of_type('function_definition') if f.start_byte <= match_byte_offset < f.end_byte]`.

For `search_target='filename'`: the evaluator is called once per matching file. `node` is the file root; the three `match_*` fields are `None` (there is no in-content match position because the regex matched the path, not file text).

### XRayNode reference

These methods and properties apply to `node` (the file root) and to any `XRayNode` you reach via `descendants_of_type`, `children`, `named_children`, `parent`, or `enclosing(...)`. The full public surface:

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

### Common patterns cookbook

Fifteen worked evaluator patterns that cover most everyday queries. Each pattern is a complete `evaluator_code` value — copy-paste it directly into your tool call.

> **v10.3.2 contract change**: Evaluators now receive `node = root` (the file's parse tree) in BOTH `content` and `filename` modes. Use `node.descendants_of_type(...)` to walk DOWN to specific constructs. The Phase 1 regex match position is exposed as `match_byte_offset` / `match_line_number` / `match_line_content` — use these to scope your descendants to the hit, e.g. `f.start_byte <= match_byte_offset < f.end_byte`. Statement-level `if`/`for`/`while`/`try` are still BANNED inside the sandbox; use comprehensions, `any()`/`all()` over generators, and `IfExp` ternaries to express the same logic.

1. **Filter regex matches inside function bodies** (the most common ask) — walk down to find functions, then check whether the Phase 1 hit landed inside one:
   ```python
   funcs = node.descendants_of_type('function_definition')
   return any(
       f.start_byte <= match_byte_offset < f.end_byte
       for f in funcs
   )
   ```

2. **Exclude docstring/comment matches** — the Phase 1 hit landed inside a comment or string node:
   ```python
   comments = node.descendants_of_type('comment')
   strings  = node.descendants_of_type('string')
   inside_comment_or_string = any(
       n.start_byte <= match_byte_offset < n.end_byte
       for n in comments + strings
   )
   return not inside_comment_or_string
   ```

3. **Walk down to a specific function by name** — find the enclosing function at the hit and inspect its name:
   ```python
   funcs = node.descendants_of_type('function_definition')
   enclosing = [
       f for f in funcs
       if f.start_byte <= match_byte_offset < f.end_byte
   ]
   return any(
       f.named_children
       and f.named_children[0].text == 'public_method'
       for f in enclosing
   )
   ```

4. **Count structural property — N elif clauses inside any function** (the canonical "find functions with N elifs" use case):
   ```python
   funcs = node.descendants_of_type('function_definition')
   return any(
       f.count_descendants_of_type('elif_clause') >= 5
       for f in funcs
   )
   ```

5. **SQL-shaped string literals at the hit position** — find the string node at the Phase 1 match and check its content:
   ```python
   strings = node.descendants_of_type('string')
   hit_strings = [
       s for s in strings
       if s.start_byte <= match_byte_offset < s.end_byte
   ]
   return any('SELECT' in s.text.upper() for s in hit_strings)
   ```

6. **Function with too many parameters** — find the function enclosing the hit and count its parameters:
   ```python
   funcs = node.descendants_of_type('function_definition')
   enclosing = [
       f for f in funcs
       if f.start_byte <= match_byte_offset < f.end_byte
   ]
   return any(
       len(f.descendants_of_type('parameter')) > 7
       for f in enclosing
   )
   ```

7. **Deep nesting detection** — total branching constructs across the file:
   ```python
   ifs    = node.count_descendants_of_type('if_statement')
   fors   = node.count_descendants_of_type('for_statement')
   whiles = node.count_descendants_of_type('while_statement')
   return (ifs + fors + whiles) >= 10
   ```

8. **List comprehension presence anywhere in the file** (showcasing post-#21 comprehension support):
   ```python
   return node.count_descendants_of_type('list_comprehension') >= 1
   ```

9. **Functions with N+ branches** — counts all branching constructs across all functions in a file (cyclomatic-complexity audit):
   ```python
   funcs = node.descendants_of_type('function_definition')
   high_complexity = [
       f for f in funcs
       if (f.count_descendants_of_type('if_statement') +
           f.count_descendants_of_type('elif_clause')) >= 8
   ]
   return len(high_complexity) > 0
   ```

10. **Returns inside `if` statements** — find files containing return statements inside conditional branches (often a code-smell for early returns or missing else cases):
    ```python
    returns = node.descendants_of_type('return_statement')
    return any(r.enclosing('if_statement') is not None for r in returns)
    ```

11. **Calls without error handling** — find files containing function calls NOT wrapped in try/except (audit risky operations):
    ```python
    calls = node.descendants_of_type('call')
    unsafe = [c for c in calls if c.enclosing('try_statement') is None]
    return len(unsafe) >= 5  # threshold tunable per audit
    ```

12. **TODO/FIXME comments** — typical pre-merge audit:
    ```python
    comments = node.descendants_of_type('comment')
    return any('TODO' in c.text or 'FIXME' in c.text for c in comments)
    ```

13. **Public functions missing return-type annotations (Python)** — `def` statements at module level without a return type annotation:
    ```python
    funcs = node.descendants_of_type('function_definition')
    public_unannotated = [
        f for f in funcs
        if f.named_children
        and not f.named_children[0].text.startswith('_')
        and not any(c.type == 'type' for c in f.named_children)
    ]
    return len(public_unannotated) > 0
    ```

14. **Bare `except:` clauses** — `except:` without an exception type silences all errors:
    ```python
    excepts = node.descendants_of_type('except_clause')
    bare = [
        e for e in excepts
        if not any(c.type in ('identifier', 'attribute', 'tuple') for c in e.named_children)
    ]
    return len(bare) > 0
    ```

15. **Classes with no docstring** — class definitions whose body's first statement is NOT a string literal. Sandbox-safe via comprehensions only (no statement-level `for`/`if`/`continue`, no `next()` since it's not in the safe-builtins list):
    ```python
    classes = node.descendants_of_type('class_definition')
    bodies_list = [
        [c for c in cls.named_children if c.type == 'block']
        for cls in classes
    ]
    has_docstring = [
        bool(bl)
        and bool(bl[0].named_children)
        and bl[0].named_children[0].type == 'expression_statement'
        and any(c.type == 'string' for c in bl[0].named_children[0].named_children)
        for bl in bodies_list
    ]
    return any(not h for h in has_docstring)
    ```

### Common cross-language node type names

tree-sitter node type names differ between languages — there is no shared vocabulary across grammars. Use this table as a starting reference for the 10 mandatory languages.

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

These are the most common types — every grammar has hundreds of node types. The fastest way to discover the exact type names for a construct is to use `xray_dump_ast` on a small example file in the language you care about, OR to consult the tree-sitter grammar repository for that language (e.g., https://github.com/tree-sitter/tree-sitter-python/blob/master/grammar.js).

### Evaluator code structure

The evaluator code is parsed as a Python **Module** (function body), NOT a bare expression. Multi-statement evaluators are first-class:

```python
# Bind intermediate variables with =, then return the final boolean.
elifs = node.count_descendants_of_type('elif_clause')
fors  = node.count_descendants_of_type('for_statement')
return (elifs + fors) >= 5
```

Rules:
- The code must end with a `return <expression>` statement that produces a boolean.
- Bind locals with `=` (`Assign`) or `+=`-style (`AugAssign`).
- A non-bool return value triggers `NonBoolReturn` in `evaluation_errors[]` — wrap with `bool(...)` if your last expression is a list/int/etc.

### Whitelisted node types

Whitelisted Python AST node types (all others are rejected before any subprocess is spawned):

- Expression core: `Call, Name, Attribute, Constant, Subscript, Compare, BoolOp, UnaryOp, List, Tuple, Dict, Return, Expr`
- Local binding: `Assign` (e.g. `x = node.named_children`), `AugAssign` (e.g. `count += 1`)
- Comprehensions and ternary: `comprehension, GeneratorExp, ListComp, SetComp, DictComp, IfExp`
- Abstract operator base classes (matched via isinstance against concrete subclasses): `boolop, cmpop, unaryop, expr_context, operator`
- Module/Load markers: `Module, Load`

Statement-level **`if` / `for` / `while` / `try`** are BANNED. Use the expression-level alternatives:
- Conditional logic → `IfExp` ternary: `result = a if cond else b`
- Iteration → comprehension: `[x for x in items if cond]`, `any(cond for x in items)`, `sum(1 for x in items if cond)`

Also banned: `class`, `def`, `import`, `lambda`, `with`, `global`, `nonlocal`.

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

## Polled Result Shape

After polling GET /api/jobs/{job_id} to COMPLETED status, result contains:

- matches[]: file_path, line_number, code_snippet, language, evaluator_decision
  - As of v10.2.0, `line_number` and `code_snippet` are populated from Phase 1 ripgrep match positions (`RegexSearchService`) for `search_target='content'` — these correspond to the same `match_line_number` / `match_line_content` values the v10.3.2 evaluator received as globals. For `search_target='filename'` searches, `line_number` is `null` and `code_snippet` is `null` because the regex matched a path, not in-content text — and the evaluator's `match_byte_offset` / `match_line_number` / `match_line_content` globals are likewise `None`. (In both modes, the evaluator's `node` global is the file root per the v10.3.2 contract.)
- evaluation_errors[]: list of per-match evaluator failures. Each entry has file_path, line_number, error_type, error_message. error_type is one of: AttributeError (evaluator referenced a node attribute that does not exist for this node type), EvaluatorTimeout (the 5s sandbox timer fired), EvaluatorCrash (subprocess exited with non-zero code), UnsupportedLanguage, NonBoolReturn. evaluation_errors does NOT cause job failure — status remains COMPLETED.
- files_processed: int — number of candidate files evaluated
- files_total: int — total candidate files found by driver
- elapsed_seconds: float
- partial: true (only on partial completion)
- timeout: true (only when job-level timeout fired)
- max_files_reached: true (only when max_files cap fired)

### evaluation_errors[] payload examples

Each error_type carries a distinct error_message shape. The examples below show the actual wire format clients receive (sourced from `src/code_indexer/xray/search_engine.py::_evaluate_file` and `src/code_indexer/xray/sandbox.py::EvalResult`):

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

Find every `prepareStatement(...)` call that is NOT inside a try-with-resources statement (Java). Walk DOWN to find method invocations, scope to the Phase 1 hit via `match_byte_offset`, then verify the enclosing try-with-resources is absent.

```json
{
  "repository_alias": "myapp-global",
  "driver_regex": "prepareStatement",
  "search_target": "content",
  "evaluator_code": "invs = node.descendants_of_type('method_invocation')\nat_hit = [i for i in invs if i.start_byte <= match_byte_offset < i.end_byte]\nreturn any(i.enclosing('try_with_resources_statement') is None for i in at_hit)",
  "include_patterns": ["*.java"]
}
```

Here we use the v10.3.2 contract: `node` is the file root, so we walk DOWN with `descendants_of_type('method_invocation')`, then narrow to invocations that contain the Phase 1 regex hit (`match_byte_offset` falls in `[start_byte, end_byte)`). For each such invocation we check `enclosing('try_with_resources_statement')` — if that returns `None`, the call has no try-with-resources guard. This filters out regex hits inside comments or strings (those won't be inside a `method_invocation` node) AND surfaces the audit signal.

## Related

- See `xray_explore` for verbose AST debug output to help craft evaluator expressions.
- `xray_explore` runs the same two-phase pipeline but adds an `ast_debug` field to every match, showing the complete tree-sitter AST node structure. Use it before writing your evaluator_code.
