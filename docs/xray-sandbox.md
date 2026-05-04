# X-Ray Sandbox Security Boundary (Epic #968 / Story #970)

This document captures the X-Ray sandbox security boundary invariants extracted from project CLAUDE.md. It defines how the AST evaluator subprocess is locked down so caller-supplied Python code cannot escape the sandbox.

`src/code_indexer/xray/sandbox.py` — `PythonEvaluatorSandbox` securely executes caller-supplied Python evaluator code against AST nodes.

**Three defense layers**:
1. AST whitelist validation (Layer 1) — `ast.parse()` + walk; any node not in `ALLOWED_NODES` is rejected before subprocess spawn.
2. Stripped exec() environment (Layer 2) — `STRIPPED_BUILTINS` removed from globals dict; only `SAFE_BUILTIN_NAMES` are available.
3. `multiprocessing.Process` isolation (Layer 3) — SIGTERM at 5.0s, SIGKILL at +1.0s; side effects confined to child.

**ALLOWED_NODES**: `Call, Name, Attribute, Constant, Subscript, Compare, BoolOp, UnaryOp, List, Tuple, Dict, Return, Expr, Module, Load` + abstract bases `boolop, cmpop, unaryop, expr_context` (match concrete subclasses via isinstance).

**STRIPPED_BUILTINS**: `getattr, setattr, delattr, hasattr, __import__, eval, exec, open, compile`.

**SAFE_BUILTIN_NAMES** (17): `len, str, int, bool, list, tuple, dict, min, max, sum, any, all, range, enumerate, zip, sorted, reversed`.

**Timeout policy**: `HARD_TIMEOUT_SECONDS=5.0` (SIGTERM), `SIGKILL_GRACE_SECONDS=1.0` (SIGKILL if still alive). Pipe data is read BEFORE `is_alive()` check — under heavy concurrency `waitpid()` races can cause `is_alive()=True` after the child has sent valid data; pipe data takes precedence.

**Why NOT signal.alarm**: FastAPI request handlers run in worker threads; `signal.alarm()` only works in the main thread.

**Four failure modes** (EvalResult.failure): `"validation_failed"`, `"evaluator_timeout"`, `"evaluator_subprocess_died"` (detail: `exitcode=N` or `no_pipe_data`), `"evaluator_returned_non_bool"` (detail: type name).

**Dunder access is BLOCKED at validation time** (Story #970 security patch — confirmed exploit closed):
- `DUNDER_ATTR_BLOCKLIST` (frozenset, 24 names) covers: `__class__`, `__bases__`, `__base__`, `__mro__`, `__subclasses__`, `__init__`, `__init_subclass__`, `__new__`, `__globals__`, `__builtins__`, `__import__`, `__dict__`, `__getattribute__`, `__setattr__`, `__delattr__`, `__reduce__`, `__reduce_ex__`, `__call__`, `__code__`, `__closure__`, `__func__`, `__module__`, `__name__`, `__qualname__`.
- Any `ast.Attribute` node whose `.attr` is in the blocklist → `validation_failed`.
- Any `ast.Subscript` node whose slice is a string `Constant` starting with `__` → `validation_failed`.
- Verified by canary tests in `tests/unit/xray/test_sandbox_dunder_escapes.py` that confirm `validation_failed` + no-subprocess + no-side-effect for each escape pattern including the confirmed exploit: `node.__class__.__init__.__globals__['__builtins__']['open']('/tmp/...','w')`.
- `type()` is intentionally absent from `SAFE_BUILTIN_NAMES` — `type(x)` raises `NameError` in the subprocess (Layer 2).

**Files**: `src/code_indexer/xray/sandbox.py`. Tests: `tests/unit/xray/test_sandbox*.py` (8 files, 112+ tests).
