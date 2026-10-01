# X-Ray Sandbox Security Boundary (v10.4.0)

This document captures the X-Ray sandbox security boundary invariants extracted from project CLAUDE.md. It defines how the AST evaluator subprocess is locked down so caller-supplied Python code cannot escape the sandbox.

**The `PythonEvaluatorSandbox` CLASS below is not the live MCP/REST evaluator contract.** It is retained in-tree but is not on the evaluation path for `xray_search` or `xray_explore` -- the current single-file evaluator contract is the Rust `fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`, documented in the [xray_search tool documentation](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md) and the [X-Ray Cookbook](xray-cookbook.md). Everything below describes this retained class's own internals, not a currently reachable API.

This module is NOT dormant, though: it also exports `validate_rust_evaluator()` (`sandbox.py:1427`), the live pre-flight validator every `xray_search`, `xray_explore`, and `xray_search_batch` request runs against caller-supplied Rust evaluator source before job submission. Only the `PythonEvaluatorSandbox` class and its subprocess pipeline, described from here on, are off the current evaluation path.

`src/code_indexer/xray/sandbox.py` — `PythonEvaluatorSandbox` securely executes caller-supplied Python evaluator code against AST nodes. The class remains in the codebase and is exercised by its own test suite; no MCP/REST request reaches the class itself.

**Three defense layers**:
1. AST whitelist validation (Layer 1) — `ast.parse()` + walk; any node not in `ALLOWED_NODES` is rejected before subprocess spawn.
2. Stripped exec() environment (Layer 2) — `STRIPPED_BUILTINS` removed from globals dict; only `SAFE_BUILTIN_NAMES` are available.
3. `multiprocessing.Process` isolation (Layer 3) — SIGTERM at 5.0s, SIGKILL at +1.0s; side effects confined to child.

**ALLOWED_NODES**:
- Core expression nodes: `Call, Name, Attribute, Constant, Subscript, Slice, Compare, BoolOp, UnaryOp, List, Tuple, Dict, Return, Expr, Module, Load`.
- Abstract bases (matched via `isinstance()`): `boolop, cmpop, unaryop, expr_context, operator`.
- Group A — local variable binding: `Assign`, `AugAssign`.
- Group B — comprehensions and ternaries: `comprehension, GeneratorExp, ListComp, IfExp`. Note: `SetComp` and `DictComp` are NOT allowed.
- Group C — statement-level control flow: `If, For, While, Break, Continue, Pass`. Iteration is bounded by `HARD_TIMEOUT_SECONDS` — infinite loops surface as `EvaluatorTimeout`, not validation rejection.
- Group E — arithmetic binary operations: `BinOp` plus the `operator` abstract base (concrete subclasses Add, Sub, Mult, Div, Mod, etc. via isinstance).
- Group G — function definitions: `FunctionDef`, `arguments`, `arg`. Allows evaluators to define helper functions. Note: `Lambda` is NOT allowed.

**STRIPPED_BUILTINS**: `getattr, setattr, delattr, __import__, eval, exec, open, compile`.

**SAFE_BUILTIN_NAMES** (8 total):
`len, any, all, range, enumerate, sorted, min, max`.

**Still banned at validation time** (rejected before any subprocess is spawned): `class`, `async def`, `lambda`, `with`, `async with`, `global`, `nonlocal`, `async`, `await`, `yield`, `yield from`, `try`/`except`/`raise` (Groups D — Try, ExceptHandler, Raise), all imports (Groups F — Import, ImportFrom, alias), set comprehensions (SetComp), dict comprehensions (DictComp). Plus dunder Attribute and Subscript access (see `DUNDER_ATTR_BLOCKLIST` below).

**Timeout policy**: `HARD_TIMEOUT_SECONDS=5.0` (SIGTERM), `SIGKILL_GRACE_SECONDS=1.0` (SIGKILL if still alive). Pipe data is read BEFORE `is_alive()` check — under heavy concurrency `waitpid()` races can cause `is_alive()=True` after the child has sent valid data; pipe data takes precedence.

**Why NOT signal.alarm**: FastAPI request handlers run in worker threads; `signal.alarm()` only works in the main thread.

**Failure modes** (`EvalResult.failure`):
- `"validation_failed"` — AST whitelist rejected the code; `detail` carries the rejection reason and a list of allowed nodes.
- `"evaluator_timeout"` — subprocess did not finish within `HARD_TIMEOUT_SECONDS`; was terminated by SIGTERM/SIGKILL.
- `"evaluator_subprocess_died"` — subprocess exited with non-zero code (segfault, NameError on stripped builtin, etc.); `detail` carries `exitcode=N` or `no_pipe_data` or `__exception__:Type:msg` (with difflib attribute suggestions for `XRayNode` typos -- `XRayNode` is this retained Python module's own AST wrapper type, distinct from the Rust `OwnedNode` used by the current MCP/REST evaluator contract).
- `"evaluator_returned_non_bool"` — legacy failure mode kept in the dataclass for backward compatibility; not emitted by the current Rust engine. This describes historical behavior of this retained module's own engine layer (`_normalize_eval_result`, `sandbox.py:858-917`), which validated the v10.4.0 dict-return contract and raised `InvalidEvaluatorReturn` in `evaluation_errors[]` instead -- not the current `_evaluate_file` in `search_engine.py`, which routes to the Rust backend and its `EvalFinding` list contract, with no dict-shape validation of this kind.

**v10.4.0 file-as-unit dict-return contract (historical, this module only)**: The sandbox accepts any return value (including `None`); shape validation is done at the engine layer, not in the sandbox. Evaluators MUST return `{"matches": [...], "value": <any>}`. Bool returns (legacy v10.3.x contract) are rejected by the engine with `InvalidEvaluatorReturn`. The sandbox passes `match_positions` (list of dicts, one per Phase 1 hit) as a global so evaluators can scope their analysis to the regex-matched positions when desired. This dict-return shape and the `match_positions` global belong to this retained Python module only -- they are not part of the current MCP/REST evaluator contract, which is the Rust `Vec<EvalFinding>` shape described in [xray_search.md](../src/code_indexer/server/mcp/tool_docs/search/xray_search.md).

**Dunder access is BLOCKED at validation time**:
- `DUNDER_ATTR_BLOCKLIST` (frozenset, 39 names — original 24 plus security-audit extensions for info-leak vectors) covers: `__class__`, `__bases__`, `__base__`, `__mro__`, `__subclasses__`, `__init__`, `__init_subclass__`, `__new__`, `__globals__`, `__builtins__`, `__import__`, `__dict__`, `__getattribute__`, `__setattr__`, `__delattr__`, `__reduce__`, `__reduce_ex__`, `__call__`, `__code__`, `__closure__`, `__func__`, `__module__`, `__name__`, `__qualname__`, plus info-leak vectors `__loader__`, `__spec__`, `__file__`, `__path__`, `__package__`, `__cached__`, `__defaults__`, `__kwdefaults__`, `__annotations__`, `__type_params__`, `__set_name__`, `__instancecheck__`, `__subclasscheck__`, `__prepare__`, `__weakref__`.
- Any `ast.Attribute` node whose `.attr` is in the blocklist → `validation_failed`.
- Any `ast.Subscript` node whose slice is a string `Constant` starting with `__` → `validation_failed`.
- Verified by canary tests in `tests/unit/xray/test_sandbox_dunder_escapes.py` that confirm `validation_failed` + no-subprocess + no-side-effect for each escape pattern including the confirmed exploit: `node.__class__.__init__.__globals__['__builtins__']['open']('/tmp/...','w')`.
**Files**: `src/code_indexer/xray/sandbox.py`. Tests: `tests/unit/xray/test_sandbox*.py` (18 files, 303+ tests).
