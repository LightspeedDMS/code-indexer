# X-Ray Sandbox Security Boundary (Epic #968 / Story #970, v10.4.0)

This document captures the X-Ray sandbox security boundary invariants extracted from project CLAUDE.md. It defines how the AST evaluator subprocess is locked down so caller-supplied Python code cannot escape the sandbox.

`src/code_indexer/xray/sandbox.py` — `PythonEvaluatorSandbox` securely executes caller-supplied Python evaluator code against AST nodes.

**Three defense layers**:
1. AST whitelist validation (Layer 1) — `ast.parse()` + walk; any node not in `ALLOWED_NODES` is rejected before subprocess spawn.
2. Stripped exec() environment (Layer 2) — `STRIPPED_BUILTINS` removed from globals dict; only `SAFE_BUILTIN_NAMES` are available.
3. `multiprocessing.Process` isolation (Layer 3) — SIGTERM at 5.0s, SIGKILL at +1.0s; side effects confined to child.

**ALLOWED_NODES** (v10.4.0 grants statement-level control flow and exception handling):
- Core expression nodes: `Call, Name, Attribute, Constant, Subscript, Compare, BoolOp, UnaryOp, List, Tuple, Dict, Return, Expr, Module, Load`.
- Abstract bases (matched via `isinstance()`): `boolop, cmpop, unaryop, expr_context, operator`.
- Group A — local variable binding: `Assign`, `AugAssign`.
- Group B — comprehensions and ternaries: `comprehension, GeneratorExp, ListComp, SetComp, DictComp, IfExp`.
- Group C — statement-level control flow (lifted in v10.4.0): `If, For, While, Break, Continue, Pass`. Iteration is bounded by `HARD_TIMEOUT_SECONDS` — infinite loops surface as `EvaluatorTimeout`, not validation rejection.
- Group D — structured exception handling (lifted in v10.4.0): `Try, ExceptHandler, Raise`.
- Group E — arithmetic binary operations (lifted in v10.4.0): `BinOp` plus the `operator` abstract base (concrete subclasses Add, Sub, Mult, Div, Mod, etc. via isinstance).

**STRIPPED_BUILTINS**: `getattr, setattr, delattr, __import__, eval, exec, open, compile`. Note: `hasattr` is intentionally in `SAFE_BUILTIN_NAMES`, not stripped — it has no escalation power beyond what the dunder blocklist already prevents.

**SAFE_BUILTIN_NAMES** (27 total in v10.4.0):
- 18 originals: `len, str, int, bool, list, tuple, dict, min, max, sum, any, all, range, enumerate, zip, sorted, reversed, hasattr`.
- 9 exception types added in v10.4.0 for `except` clauses in Group D try/except blocks: `Exception, ValueError, TypeError, RuntimeError, AttributeError, KeyError, IndexError, NameError, StopIteration`. These are read-only references; none grant escalation power beyond what the dunder blocklist enforces.

**Still banned at validation time** (rejected before any subprocess is spawned): `def`, `async def`, `class`, `lambda`, `import`, `from ... import`, `with`, `async with`, `global`, `nonlocal`, `async`, `await`, `yield`, `yield from`. Plus dunder Attribute and Subscript access (see `DUNDER_ATTR_BLOCKLIST` below).

**Timeout policy**: `HARD_TIMEOUT_SECONDS=5.0` (SIGTERM), `SIGKILL_GRACE_SECONDS=1.0` (SIGKILL if still alive). Pipe data is read BEFORE `is_alive()` check — under heavy concurrency `waitpid()` races can cause `is_alive()=True` after the child has sent valid data; pipe data takes precedence.

**Why NOT signal.alarm**: FastAPI request handlers run in worker threads; `signal.alarm()` only works in the main thread.

**Failure modes** (`EvalResult.failure`):
- `"validation_failed"` — AST whitelist rejected the code; `detail` carries the rejection reason and a list of allowed nodes.
- `"evaluator_timeout"` — subprocess did not finish within `HARD_TIMEOUT_SECONDS`; was terminated by SIGTERM/SIGKILL.
- `"evaluator_subprocess_died"` — subprocess exited with non-zero code (segfault, NameError on stripped builtin, etc.); `detail` carries `exitcode=N` or `no_pipe_data` or `__exception__:Type:msg` (with difflib attribute suggestions for XRayNode typos).
- `"evaluator_returned_non_bool"` — legacy failure mode kept in the dataclass for backward compatibility; no longer emitted by the current engine. The engine layer (`_evaluate_file`) now validates the v10.4.0 dict-return contract and raises `InvalidEvaluatorReturn` in `evaluation_errors[]` instead.

**v10.4.0 file-as-unit dict-return contract**: The sandbox accepts any return value (including `None`); shape validation is done at the engine layer, not in the sandbox. Evaluators MUST return `{"matches": [...], "value": <any>}`. Bool returns (legacy v10.3.x contract) are rejected by the engine with `InvalidEvaluatorReturn`. The sandbox passes `match_positions` (list of dicts, one per Phase 1 hit) as a global so evaluators can scope their analysis to the regex-matched positions when desired.

**Dunder access is BLOCKED at validation time** (Story #970 security patch — confirmed exploit closed):
- `DUNDER_ATTR_BLOCKLIST` (frozenset, 39 names — original 24 plus security-audit extensions for info-leak vectors) covers: `__class__`, `__bases__`, `__base__`, `__mro__`, `__subclasses__`, `__init__`, `__init_subclass__`, `__new__`, `__globals__`, `__builtins__`, `__import__`, `__dict__`, `__getattribute__`, `__setattr__`, `__delattr__`, `__reduce__`, `__reduce_ex__`, `__call__`, `__code__`, `__closure__`, `__func__`, `__module__`, `__name__`, `__qualname__`, plus info-leak vectors `__loader__`, `__spec__`, `__file__`, `__path__`, `__package__`, `__cached__`, `__defaults__`, `__kwdefaults__`, `__annotations__`, `__type_params__`, `__set_name__`, `__instancecheck__`, `__subclasscheck__`, `__prepare__`, `__weakref__`.
- Any `ast.Attribute` node whose `.attr` is in the blocklist → `validation_failed`.
- Any `ast.Subscript` node whose slice is a string `Constant` starting with `__` → `validation_failed`.
- Verified by canary tests in `tests/unit/xray/test_sandbox_dunder_escapes.py` that confirm `validation_failed` + no-subprocess + no-side-effect for each escape pattern including the confirmed exploit: `node.__class__.__init__.__globals__['__builtins__']['open']('/tmp/...','w')`.
- `type()` is intentionally absent from `SAFE_BUILTIN_NAMES` — `type(x)` raises `NameError` in the subprocess (Layer 2).

**Files**: `src/code_indexer/xray/sandbox.py`. Tests: `tests/unit/xray/test_sandbox*.py` (8 files, 112+ tests).
