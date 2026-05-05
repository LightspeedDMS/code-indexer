"""PythonEvaluatorSandbox: secure execution of caller-supplied Python evaluator code.

Three defense layers:
  1. Stdlib ``ast`` whitelist validation — rejects any node type outside the
     allowed set before any subprocess is spawned.
  2. Stripped exec() environment — removes dangerous builtins (getattr,
     setattr, delattr, __import__, eval, exec, open, compile) from
     the globals dict passed to exec().  Note: ``hasattr`` is in
     SAFE_BUILTIN_NAMES (not stripped) because it has no escalation
     power beyond what the dunder blocklist already prevents.
  3. multiprocessing.Process isolation with hard 5.0 s SIGTERM + 1.0 s
     SIGKILL escalation — protects the parent against infinite loops,
     C-level crashes, and unbounded resource consumption.

``signal.alarm()`` is explicitly NOT used because FastAPI request handlers
run in worker threads, and signal.alarm() only works in the main thread.

Usage::

    from code_indexer.xray.sandbox import PythonEvaluatorSandbox, EvalResult
    from code_indexer.xray.ast_engine import AstSearchEngine

    engine = AstSearchEngine()
    root = engine.parse(source_code, "java")
    node = root.named_children[0]

    sb = PythonEvaluatorSandbox()
    result: EvalResult = sb.run(
        "return node.type == 'method_invocation'",
        node=node,
        root=root,
        source=source_code,
        lang="java",
        file_path="/src/Foo.java",
    )
    if result.failure is None:
        matched = result.value  # bool
"""

from __future__ import annotations

import ast
import builtins
import difflib
import multiprocessing
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from code_indexer.xray.xray_node import XRayNode


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """Result of static analysis validation of evaluator code."""

    ok: bool
    reason: Optional[str] = field(default=None)


@dataclass
class EvalResult:
    """Result of running evaluator code in a sandboxed subprocess.

    Exactly one of ``value`` (success) or ``failure`` (error) is meaningful:

    - ``failure is None`` — success; ``value`` holds the evaluator return value.
      In the file-as-unit contract (v10.4.0+), ``value`` is a dict with shape
      ``{"matches": [...], "value": <anything>}``.  In the legacy bool contract
      (now rejected at the engine layer), ``value`` was a bool.
    - ``failure == "validation_failed"`` — static analysis rejected the code;
      ``detail`` contains the rejection reason.
    - ``failure == "evaluator_timeout"`` — subprocess did not finish within
      ``HARD_TIMEOUT_SECONDS``; it was terminated.
    - ``failure == "evaluator_subprocess_died"`` — subprocess exited with
      non-zero code (segfault, NameError on stripped builtin, etc.);
      ``detail`` carries ``exitcode=N`` or ``no_pipe_data``.
    - ``failure == "evaluator_returned_non_bool"`` — legacy failure mode kept
      for backward compatibility; no longer emitted by current engine.
    """

    value: Optional[Any] = field(default=None)
    failure: Optional[str] = field(default=None)
    detail: Optional[str] = field(default=None)


# ---------------------------------------------------------------------------
# Safe builtins allowlist
# ---------------------------------------------------------------------------

SAFE_BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        "len",
        "str",
        "int",
        "bool",
        "list",
        # Exception types — needed for ``except Exception:`` / ``except ValueError:``
        # in try/except blocks (Group D, v10.4.0).  These are read-only references;
        # none grant escalation power beyond what the dunder blocklist prevents.
        "Exception",
        "ValueError",
        "TypeError",
        "RuntimeError",
        "AttributeError",
        "KeyError",
        "IndexError",
        "NameError",
        "StopIteration",
        "tuple",
        "dict",
        "min",
        "max",
        "sum",
        "any",
        "all",
        "range",
        "enumerate",
        "zip",
        "sorted",
        "reversed",
        # hasattr is safe: it is equivalent to try/getattr/except AttributeError
        # and has no escalation power beyond the dunder blocklist already enforced
        # at AST validation time (M1 Codex review finding).
        "hasattr",
    }
)


# ---------------------------------------------------------------------------
# Validation hint strings (improvement A — field-feedback fix #18)
# ---------------------------------------------------------------------------

# Maps the *first* forbidden AST node type name encountered during validation
# to a human-readable workaround hint.  Keys are exact type.__name__ strings.
_VALIDATION_HINTS: dict[str, str] = {
    "Lambda": (
        "Lambdas are not allowed. Inline the boolean expression directly, "
        "or assign with `=` to a local variable."
    ),
    "FunctionDef": (
        "Function and class definitions are not allowed. Evaluator code must be "
        "a single expression or sequence of statements that produces a return value."
    ),
    "AsyncFunctionDef": (
        "Function and class definitions are not allowed. Evaluator code must be "
        "a single expression or sequence of statements that produces a return value."
    ),
    "ClassDef": (
        "Function and class definitions are not allowed. Evaluator code must be "
        "a single expression or sequence of statements that produces a return value."
    ),
    "Import": (
        "Imports are not allowed. "
        "Available builtins: len, str, int, bool, list, tuple, dict, "
        "min, max, sum, any, all, range, enumerate, zip, sorted, reversed, hasattr."
    ),
    "ImportFrom": (
        "Imports are not allowed. "
        "Available builtins: len, str, int, bool, list, tuple, dict, "
        "min, max, sum, any, all, range, enumerate, zip, sorted, reversed, hasattr."
    ),
    "Global": (
        "Global/nonlocal declarations are not allowed. Use local `=` assignments only."
    ),
    "Nonlocal": (
        "Global/nonlocal declarations are not allowed. Use local `=` assignments only."
    ),
}

# Public attributes and methods of XRayNode used by difflib for suggestions
# (improvement B — field-feedback fix #18).
_XRAY_NODE_PUBLIC_ATTRS: frozenset[str] = frozenset(
    {
        # Properties
        "parent",
        "children",
        "named_children",
        "type",
        "text",
        "start_byte",
        "end_byte",
        "start_point",
        "end_point",
        "child_count",
        "named_child_count",
        "is_named",
        "has_error",
        # Methods
        "is_descendant_of",
        "descendants_of_type",
        "count_descendants_of_type",
        "enclosing",
        "child_by_field_name",
        "children_by_field_name",
    }
)

# Minimum similarity score (0-1) for difflib to emit a suggestion.
_ATTR_SUGGESTION_CUTOFF: float = 0.6


# ---------------------------------------------------------------------------
# Subprocess worker (module-level so it is picklable for spawn context)
# ---------------------------------------------------------------------------


def _run_evaluator(
    code: str,
    node: "XRayNode",
    root: "XRayNode",
    source: str,
    lang: str,
    file_path: str,
    conn: Any,
    match_byte_offset: Optional[int] = None,
    match_line_number: Optional[int] = None,
    match_line_content: Optional[str] = None,
    match_positions: Optional[list] = None,
) -> None:
    """Execute *code* inside a stripped-builtin environment and send result via *conn*.

    This function runs inside a forked/spawned subprocess.  All imports needed
    here must be available in the child process (they are, via fork inheritance).

    In the file-as-unit contract (v10.4.0+), ``match_positions`` is the list of
    all Phase 1 regex hits for the file: each entry is a dict with keys
    ``line_number``, ``line_content``, ``column``, ``byte_offset``.
    The evaluator is expected to return a dict ``{"matches": [...], "value": ...}``.
    The raw return value is sent over the pipe (not coerced to bool).
    """
    try:
        # Build safe builtins from the canonical builtins module — not from
        # __builtins__ which may be a module (not dict) depending on context.
        all_builtins: dict[str, Any] = vars(builtins)
        stripped = PythonEvaluatorSandbox.STRIPPED_BUILTINS
        safe: dict[str, Any] = {
            k: v
            for k, v in all_builtins.items()
            if k in SAFE_BUILTIN_NAMES and k not in stripped
        }

        globals_dict: dict[str, Any] = {
            "__builtins__": safe,
            "node": node,
            "root": root,
            "source": source,
            "lang": lang,
            "file_path": file_path,
            # Legacy per-position globals (kept for compat, now always None)
            "match_byte_offset": match_byte_offset,
            "match_line_number": match_line_number,
            "match_line_content": match_line_content,
            # File-as-unit: all Phase 1 hits as a list of dicts
            "match_positions": match_positions if match_positions is not None else [],
        }
        locals_dict: dict[str, Any] = {}

        # Wrap user code inside a function so ``return`` works at the top level
        wrapped = "def __evaluator__():\n" + textwrap.indent(code, "    ")
        exec(wrapped, globals_dict, locals_dict)  # noqa: S102
        result = locals_dict["__evaluator__"]()
        # Send the raw result — engine layer validates dict shape and enriches.
        conn.send(result)
    except AttributeError as exc:  # noqa: BLE001
        # Augment AttributeError with difflib suggestions for XRayNode typos.
        base_msg = str(exc)
        # Extract the bad attribute name from messages like
        # "'XRayNode' object has no attribute 'children_named'"
        suggestion_suffix = ""
        if "has no attribute" in base_msg:
            parts = base_msg.rsplit("'", 2)
            # parts[-2] is the bad attribute name when the string ends with "'name'"
            bad_attr = parts[-2] if len(parts) >= 2 else ""
            if bad_attr:
                matches = difflib.get_close_matches(
                    bad_attr,
                    _XRAY_NODE_PUBLIC_ATTRS,
                    n=3,
                    cutoff=_ATTR_SUGGESTION_CUTOFF,
                )
                if matches:
                    suggestion_suffix = f" Did you mean: {', '.join(matches)}?"
        conn.send(f"__exception__:AttributeError:{base_msg}{suggestion_suffix}")
    except Exception as exc:  # noqa: BLE001
        conn.send(f"__exception__:{type(exc).__name__}:{exc}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


class PythonEvaluatorSandbox:
    """Securely execute caller-supplied Python evaluator code against AST nodes.

    Thread-safe: each call to ``run()`` spawns an independent subprocess with
    its own ``multiprocessing.Pipe``.  Multiple ThreadPoolExecutor workers may
    call ``run()`` concurrently without sharing any mutable state.
    """

    # Wall-clock timeout budget for subprocess execution
    HARD_TIMEOUT_SECONDS: float = 5.0

    # Grace period between SIGTERM and SIGKILL
    SIGKILL_GRACE_SECONDS: float = 1.0

    # Allowed Python AST node types (whitelist).
    # ``isinstance()`` is used for the check, so abstract base classes
    # (ast.boolop, ast.cmpop, ast.unaryop, ast.expr_context, ast.operator)
    # correctly match their concrete subclasses (Eq, And, Not, Load, Add, etc.).
    #
    # Statement-level control flow allowed: If, For, While, Break, Continue, Pass.
    # Comprehensions AND for-statements are both allowed.
    #
    # Loop termination is bounded by HARD_TIMEOUT_SECONDS — infinite loops result
    # in EvaluatorTimeout, not in validation rejection.  Safety belts at AST
    # validation are intentionally minimal; the subprocess timeout is the
    # authoritative termination guarantee.
    #
    # Local variables via ``=`` and ``+=`` are allowed; ``global`` and
    # ``nonlocal`` declarations are NOT.
    ALLOWED_NODES: tuple[type, ...] = (
        ast.Call,
        ast.Name,
        ast.Attribute,
        ast.Constant,
        ast.Subscript,
        ast.Compare,
        ast.BoolOp,
        ast.UnaryOp,
        ast.List,
        ast.Tuple,
        ast.Dict,
        ast.Return,
        ast.Expr,
        ast.Module,
        ast.Load,
        # Abstract operator base classes — concrete subclasses (Eq, And,
        # Not, Load, Store, …) are accepted via isinstance()
        ast.boolop,
        ast.cmpop,
        ast.unaryop,
        ast.expr_context,
        # Group A — local variable binding
        # Assign: allows ``x = node.named_children`` then reuse ``x``
        ast.Assign,
        # AugAssign: allows ``count += 1`` for accumulation patterns;
        # requires ast.operator for the operator node (Add, Sub, etc.)
        ast.AugAssign,
        ast.operator,  # abstract base: Add, Sub, Mult, Div, BitOr, etc.
        # Group B — comprehensions and ternaries (highest usability impact)
        # comprehension: the ``for x in y if z`` clause inside any comprehension
        ast.comprehension,
        # Comprehension expression forms — all share the same clause node
        ast.GeneratorExp,  # (x for x in items)
        ast.ListComp,      # [x for x in items]
        ast.SetComp,       # {x for x in items}
        ast.DictComp,      # {k: v for k, v in items}
        # IfExp: ternary expression — ``a if cond else b``
        ast.IfExp,
        # Group C — statement-level control flow
        # Infinite loops are bounded by HARD_TIMEOUT_SECONDS subprocess kill;
        # misbehaving evaluators produce EvaluatorTimeout, not validation rejection.
        ast.If,        # statement-level branching: if/elif/else blocks
        ast.For,       # statement-level iteration: for x in iterable
        ast.While,     # statement-level iteration: while condition (bounded by timeout)
        ast.Break,     # early loop exit
        ast.Continue,  # skip current iteration
        ast.Pass,      # empty body placeholder: if cond: pass
        # Group D — structured exception handling (Directive D, v10.4.0)
        # try/except/finally lets evaluators handle per-node errors gracefully.
        # raise lets evaluators surface clean errors — produces EvaluatorCrash,
        # not validation_failed.
        ast.Try,           # try/except/finally blocks
        ast.ExceptHandler, # except clauses (bare and typed)
        ast.Raise,         # raise statements
        # Group E — arithmetic binary operations
        # BinOp allows x + n, x - n, x * n etc. in evaluator code.
        # ast.operator covers concrete subclasses Add, Sub, Mult, Div etc.
        # via isinstance() — same pattern as ast.boolop, ast.cmpop.
        ast.BinOp,         # binary arithmetic: x + y, x * y, etc.
        ast.operator,      # abstract base for Add, Sub, Mult, Div, Mod, etc.
    )

    # Dunder attribute names that are blocked at AST validation time.
    # Accessing any of these via Attribute or Subscript is a known sandbox
    # escape vector — they allow reaching real module globals / builtins.
    DUNDER_ATTR_BLOCKLIST: frozenset[str] = frozenset(
        {
            # --- original 24 entries ---
            "__class__",
            "__bases__",
            "__base__",
            "__mro__",
            "__subclasses__",
            "__init__",
            "__init_subclass__",
            "__new__",
            "__globals__",
            "__builtins__",
            "__import__",
            "__dict__",
            "__getattribute__",
            "__setattr__",
            "__delattr__",
            "__reduce__",
            "__reduce_ex__",
            "__call__",
            "__code__",
            "__closure__",
            "__func__",
            "__module__",
            "__name__",
            "__qualname__",
            # --- security audit extensions: info-leak vectors ---
            "__loader__",  # module loader — could read source files
            "__spec__",  # module spec — origin file path leak
            "__file__",  # module path leak
            "__path__",  # package path leak
            "__package__",  # package name leak
            "__cached__",  # bytecode cache path leak
            "__defaults__",  # captured function defaults
            "__kwdefaults__",  # captured kwarg defaults
            "__annotations__",  # type hint leak
            "__type_params__",  # Py 3.12+ generic syntax leak (forward-compat)
            "__set_name__",  # descriptor protocol abuse
            "__instancecheck__",  # metaclass abuse
            "__subclasscheck__",  # metaclass abuse
            "__prepare__",  # metaclass namespace access
            "__weakref__",  # weakref slot access
        }
    )

    # Builtins removed from the exec() environment.
    # Note: hasattr is in SAFE_BUILTIN_NAMES (not here) — see M1 rationale
    # in module docstring and SAFE_BUILTIN_NAMES comment above.
    STRIPPED_BUILTINS: frozenset[str] = frozenset(
        {
            "getattr",
            "setattr",
            "delattr",
            "__import__",
            "eval",
            "exec",
            "open",
            "compile",
        }
    )

    # ---------------------------------------------------------------------------
    # Internal class helpers
    # ---------------------------------------------------------------------------

    @classmethod
    def _allowed_node_names(cls) -> list[str]:
        """Return a sorted list of concrete AST node type names from ALLOWED_NODES."""
        names: list[str] = []
        for node_type in cls.ALLOWED_NODES:
            name = node_type.__name__
            # Skip abstract base classes whose names are lowercase (boolop, cmpop, …)
            if name[0].isupper():
                names.append(name)
        return sorted(set(names))

    @classmethod
    def _build_rejection_reason(cls, forbidden_name: str) -> str:
        """Build a rich, actionable rejection message for a forbidden AST node type.

        Includes:
        - The forbidden node type name
        - A workaround hint (when one is registered in _VALIDATION_HINTS)
        - The full list of whitelisted node names
        - A pointer to evaluator API documentation
        """
        parts: list[str] = [f"'{forbidden_name}' is not allowed in evaluator code."]

        hint = _VALIDATION_HINTS.get(forbidden_name)
        if hint:
            parts.append(hint)

        allowed = ", ".join(cls._allowed_node_names())
        parts.append(f"Whitelisted nodes: {allowed}.")
        parts.append(
            "See evaluator API documentation for the full whitelist and usage examples."
        )

        return " ".join(parts)

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def validate(self, code: str) -> ValidationResult:
        """Statically validate *code* against the AST whitelist.

        Parses *code* with ``ast.parse(mode='exec')`` and walks every node.
        If any node type is not in ``ALLOWED_NODES``, validation fails with
        a descriptive reason naming the rejected node type.

        Returns:
            ValidationResult with ``ok=True`` if all nodes are whitelisted,
            or ``ok=False`` with ``reason`` describing the first violation.
        """
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError as exc:
            return ValidationResult(ok=False, reason=f"syntax_error: {exc}")
        except ValueError as exc:
            # ast.parse raises ValueError for inputs such as null bytes that
            # are structurally invalid before parsing even begins.
            return ValidationResult(ok=False, reason=f"value_error: {exc}")

        for node in ast.walk(tree):
            if not isinstance(node, self.ALLOWED_NODES):
                return ValidationResult(
                    ok=False,
                    reason=self._build_rejection_reason(type(node).__name__),
                )

            # Block Attribute access to dunder names (Python sandbox escape vector).
            # e.g. node.__class__, ''.join.__globals__, dict.__base__
            if (
                isinstance(node, ast.Attribute)
                and node.attr in self.DUNDER_ATTR_BLOCKLIST
            ):
                return ValidationResult(
                    ok=False,
                    reason=(
                        f"Attribute access to {node.attr!r} blocked "
                        f"(sandbox escape vector)"
                    ),
                )

            # Block Subscript access using a string Constant slice that is a dunder
            # or underscore name.
            # e.g. globals()['__import__'], whatever['__builtins__']
            # Python 3.9+: Subscript.slice is the value directly (no Index wrapper).
            # Python <3.9: slice is ast.Index(...) — handle defensively.
            if isinstance(node, ast.Subscript):
                sl = node.slice
                # Unwrap legacy ast.Index wrapper (Python <3.9 compatibility)
                if hasattr(sl, "value") and not isinstance(sl, ast.Constant):
                    sl = sl.value  # type: ignore[assignment]
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    if (
                        sl.value.startswith("__")
                        or sl.value in self.DUNDER_ATTR_BLOCKLIST
                    ):
                        return ValidationResult(
                            ok=False,
                            reason=(
                                f"Subscript access to {sl.value!r} blocked "
                                f"(sandbox escape vector)"
                            ),
                        )

        return ValidationResult(ok=True)

    def run(
        self,
        code: str,
        *,
        node: "XRayNode",
        root: "XRayNode",
        source: str,
        lang: str,
        file_path: str,
        match_byte_offset: Optional[int] = None,
        match_line_number: Optional[int] = None,
        match_line_content: Optional[str] = None,
        match_positions: Optional[list] = None,
    ) -> EvalResult:
        """Run *code* in a sandboxed subprocess and return the result.

        Validates *code* first — if validation fails the subprocess is never
        spawned.  On success the subprocess receives SIGTERM after
        ``HARD_TIMEOUT_SECONDS`` and SIGKILL after an additional
        ``SIGKILL_GRACE_SECONDS`` if it has not already exited.

        Args:
            code: Evaluator source code (must pass ``validate()``).
            node: File root AST node (XRayNode) — always the module/file root.
                Evaluators walk DOWN from this node via ``descendants_of_type``.
            root: Alias for *node* (same object); kept for backward compatibility.
            source: Full source text of the file being evaluated.
            lang: Language identifier string (e.g. ``"java"``, ``"python"``).
            file_path: Absolute path of the file being evaluated.
            match_byte_offset: Byte offset of the Phase 1 regex match within the
                file source.  ``None`` in filename-target mode.
            match_line_number: 1-based line number of the Phase 1 regex match.
                ``None`` in filename-target mode.
            match_line_content: Text of the line that matched the Phase 1 regex.
                ``None`` in filename-target mode.

        Returns:
            EvalResult — see class docstring for failure mode details.
        """
        validation = self.validate(code)
        if not validation.ok:
            return EvalResult(failure="validation_failed", detail=validation.reason)

        ctx = self._get_mp_context()
        parent_conn, child_conn = ctx.Pipe(duplex=False)

        proc = ctx.Process(
            target=_run_evaluator,
            args=(
                code,
                node,
                root,
                source,
                lang,
                file_path,
                child_conn,
                match_byte_offset,
                match_line_number,
                match_line_content,
                match_positions,
            ),
            daemon=True,
        )
        proc.start()
        # Close child end in parent so EOF is detected properly
        child_conn.close()

        proc.join(timeout=self.HARD_TIMEOUT_SECONDS)

        # Poll for pipe data BEFORE checking is_alive().
        #
        # Under heavy concurrency on Linux, waitpid() races between threads
        # can cause proc.is_alive() to return True even after the child has
        # completed its work and sent data over the pipe.  Pipe data is the
        # authoritative signal: if the child sent a result, honour it
        # regardless of is_alive() or exitcode.
        #
        # If no data is available AND the process is still alive, it is a
        # genuine timeout — terminate it with SIGTERM then SIGKILL.
        has_data = parent_conn.poll(timeout=0.0)

        if not has_data and proc.is_alive():
            proc.terminate()
            proc.join(timeout=self.SIGKILL_GRACE_SECONDS)
            if proc.is_alive():
                proc.kill()
                proc.join()
            parent_conn.close()
            return EvalResult(failure="evaluator_timeout")

        if not has_data:
            parent_conn.close()
            exitcode = proc.exitcode
            if exitcode is not None and exitcode != 0:
                return EvalResult(
                    failure="evaluator_subprocess_died",
                    detail=f"exitcode={exitcode}",
                )
            return EvalResult(
                failure="evaluator_subprocess_died",
                detail="no_pipe_data",
            )

        try:
            raw = parent_conn.recv()
        except EOFError:
            parent_conn.close()
            return EvalResult(
                failure="evaluator_subprocess_died",
                detail="no_pipe_data",
            )
        parent_conn.close()

        # Ensure the subprocess is fully reaped (no zombie) — non-blocking.
        if proc.is_alive():
            proc.join(timeout=0.0)

        # Subprocess sends __exception__:Type:msg on internal error
        if isinstance(raw, str) and raw.startswith("__exception__:"):
            return EvalResult(
                failure="evaluator_subprocess_died",
                detail=raw,
            )

        # Accept any value — dict (new file-as-unit contract) or bool (legacy).
        # The engine layer (_evaluate_file) validates the dict shape and enriches.
        return EvalResult(value=raw)

    # ---------------------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _get_mp_context() -> Any:
        """Return the best available multiprocessing context.

        Prefers ``fork`` on Linux/macOS for minimal spawn overhead.
        Falls back to ``spawn`` on platforms where fork is unavailable.
        """
        try:
            return multiprocessing.get_context("fork")
        except ValueError:
            return multiprocessing.get_context("spawn")
