"""PythonEvaluatorSandbox: secure execution of caller-supplied Python evaluator code.

Three defense layers:
  1. Stdlib ``ast`` whitelist validation — rejects any node type outside the
     allowed set before any subprocess is spawned.
  2. Stripped exec() environment — removes dangerous builtins (getattr,
     setattr, delattr, __import__, eval, exec, open, compile) from
     the globals dict passed to exec().
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
import re as _re
import textwrap
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

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
    error_code: Optional[str] = field(default=None)
    offending_construct: Optional[str] = field(default=None)
    offending_line: Optional[int] = field(default=None)


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
        "any",
        "all",
        "range",
        "enumerate",
        "sorted",
        "min",
        "max",
    }
)


# ---------------------------------------------------------------------------
# Validation hint strings (improvement A — field-feedback fix #18)
# ---------------------------------------------------------------------------

# Maps the *first* forbidden AST node type name encountered during validation
# to a human-readable workaround hint.  Keys are exact type.__name__ strings.
_VALIDATION_HINTS: dict[str, str] = {
    "AsyncFunctionDef": (
        "Function and class definitions are not allowed. Evaluator code must be "
        "a single expression or sequence of statements that produces a return value."
    ),
    "ClassDef": (
        "Function and class definitions are not allowed. Evaluator code must be "
        "a single expression or sequence of statements that produces a return value."
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
        # Story #993 Improvement 2 additions
        "is_in_try_resources",
        "enclosing_method_body",
        "node_at_byte_offset",
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
        # Slice expressions inside Subscript: source[10:20], source[-30:], lines[0:10:2]
        ast.Slice,
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
        ast.ListComp,  # [x for x in items]
        # IfExp: ternary expression — ``a if cond else b``
        ast.IfExp,
        # Group C — statement-level control flow
        # Infinite loops are bounded by HARD_TIMEOUT_SECONDS subprocess kill;
        # misbehaving evaluators produce EvaluatorTimeout, not validation rejection.
        ast.If,  # statement-level branching: if/elif/else blocks
        ast.For,  # statement-level iteration: for x in iterable
        ast.While,  # statement-level iteration: while condition (bounded by timeout)
        ast.Break,  # early loop exit
        ast.Continue,  # skip current iteration
        ast.Pass,  # empty body placeholder: if cond: pass
        # Group E — arithmetic binary operations
        # BinOp allows x + n, x - n, x * n etc. in evaluator code.
        # ast.operator covers concrete subclasses Add, Sub, Mult, Div etc.
        # via isinstance() — already included in Group A above.
        ast.BinOp,  # binary arithmetic: x + y, x * y, etc.
        # Group G — function definitions (transpilable subset)
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
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
            return ValidationResult(
                ok=False,
                reason=f"syntax_error: {exc}",
                error_code="syntax_error",
                offending_line=getattr(exc, "lineno", None),
            )
        except ValueError as exc:
            # ast.parse raises ValueError for inputs such as null bytes that
            # are structurally invalid before parsing even begins.
            return ValidationResult(
                ok=False,
                reason=f"value_error: {exc}",
                error_code="value_error",
            )

        for node in ast.walk(tree):
            if not isinstance(node, self.ALLOWED_NODES):
                return ValidationResult(
                    ok=False,
                    reason=self._build_rejection_reason(type(node).__name__),
                    error_code="forbidden_node",
                    offending_construct=type(node).__name__,
                    offending_line=getattr(node, "lineno", None),
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
                    error_code="dunder_blocked",
                    offending_construct=node.attr,
                    offending_line=getattr(node, "lineno", None),
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
                            error_code="dunder_blocked",
                            offending_construct=sl.value,
                            offending_line=getattr(node, "lineno", None),
                        )

            # v10.4.3: Slice expressions (e.g. obj['__class__':10]) can hide dunder
            # strings inside Slice.lower/upper/step. The original Constant-only check
            # above misses these because the slice node is ast.Slice, not ast.Constant.
            # Defense-in-depth: same dunder rule applies to all three components.
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
                sl_node = node.slice
                for component_name, component in (
                    ("lower", sl_node.lower),
                    ("upper", sl_node.upper),
                    ("step", sl_node.step),
                ):
                    if (
                        component is not None
                        and isinstance(component, ast.Constant)
                        and isinstance(component.value, str)
                        and (
                            component.value.startswith("__")
                            or component.value in self.DUNDER_ATTR_BLOCKLIST
                        )
                    ):
                        return ValidationResult(
                            ok=False,
                            reason=(
                                f"Slice {component_name} access to {component.value!r} "
                                f"blocked (sandbox escape vector)"
                            ),
                            error_code="dunder_blocked",
                            offending_construct=component.value,
                            offending_line=getattr(node, "lineno", None),
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
            file_path: Path of the file being evaluated (relative to repo root in production).
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

    def run_batch(
        self,
        *,
        evaluator_code: str,
        file_specs: List[Dict[str, Any]],
        worker_threads: int = 2,
        timeout_seconds: int = 120,
        ast_engine: Optional[Any] = None,
        on_process_spawned: Optional[Callable] = None,
    ) -> List[
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
    ]:
        """Run evaluator_code against a batch of files.

        When ``ast_engine`` is provided, runs inline in the parent process:
        parses each file with ``ast_engine.parse()``, then calls ``self.run()``
        per file using a ThreadPoolExecutor.  This mode allows test patches on
        ``sandbox.run`` and ``ast_engine.parse`` to be intercepted.
        ``timeout_seconds`` is enforced as a wall-clock cap in both modes.

        When ``ast_engine`` is ``None`` (default), spawns a single driver
        subprocess that handles all files; the driver starts clean (no HNSW
        cache) via the spawn multiprocessing context.

        Validates code once in the parent.  On validation failure returns
        per-file error tuples without spawning or processing.

        Args:
            evaluator_code: Python evaluator source (same contract as run()).
            file_specs: Dicts with keys: file_path, source, lang, match_positions
                (WITHOUT ast_node — populated from byte_offset during processing).
            worker_threads: Concurrency; must be >= 1.
            timeout_seconds: Wall-clock cap for the entire batch; must be > 0.
                Enforced in both inline and driver-subprocess modes.
            ast_engine: Optional AstSearchEngine instance.  When provided,
                processing runs inline in the caller's process (no subprocess
                driver is spawned), allowing patches on ``sandbox.run`` and
                ``ast_engine.parse`` to be intercepted by callers.

        Returns:
            List of (matches, errors, meta) tuples, one per file spec, in order.
        """
        if worker_threads < 1:
            raise ValueError(f"worker_threads must be >= 1, got {worker_threads}")
        if timeout_seconds <= 0:
            raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
        if not file_specs:
            return []

        validation = self.validate(evaluator_code)
        if not validation.ok:
            reason = validation.reason or "Evaluator validation failed"
            return [
                _batch_error(spec.get("file_path", ""), "ValidationFailed", reason)
                for spec in file_specs
            ]

        if ast_engine is not None:
            return _run_inline_batch(
                self,
                file_specs,
                evaluator_code,
                worker_threads,
                timeout_seconds,
                ast_engine,
            )

        return _run_driver_batch(
            self,
            file_specs,
            evaluator_code,
            worker_threads,
            timeout_seconds,
            on_process_spawned=on_process_spawned,
        )


# ---------------------------------------------------------------------------
# Spawn-driver architecture (module-level for picklability)
# ---------------------------------------------------------------------------


def _batch_error(
    file_path: str,
    error_type: str,
    message: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return a single ([], [error_dict], None) batch result tuple."""
    return (
        [],
        [
            {
                "file_path": file_path,
                "line_number": 0,
                "error_type": error_type,
                "error_message": message,
            }
        ],
        None,
    )


def _line_to_byte_offset_bytes(source_str: str, line_number: int) -> int:
    """Convert 1-indexed line_number to UTF-8 byte offset of that line's start.

    Uses encoded byte lengths so the result is correct for non-ASCII sources.
    """
    if line_number <= 1:
        return 0
    lines = source_str.split("\n")
    if line_number > len(lines):
        return len(source_str.encode("utf-8"))
    return sum(len(line.encode("utf-8")) + 1 for line in lines[: line_number - 1])


def _build_matches_from_evaluator(
    raw_matches: List[Any],
    file_path: str,
    lang: str,
    source_lines: List[str],
) -> Tuple[
    List[Dict[str, Any]],
    Optional[
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
    ],
]:
    """Build enriched match dicts from raw evaluator match list.

    Returns (matches, error_tuple_or_none).  On first malformed entry returns
    ([], error_tuple) so the caller can short-circuit.
    """
    matches: List[Dict[str, Any]] = []
    for em in raw_matches:
        if not isinstance(em, dict):
            continue
        if "line_number" not in em:
            return [], _batch_error(
                file_path,
                "InvalidEvaluatorReturn",
                "each match must contain 'line_number'",
            )
        try:
            ln = int(em["line_number"])
        except (TypeError, ValueError):
            return [], _batch_error(
                file_path,
                "InvalidEvaluatorReturn",
                f"line_number must be an int, got {em['line_number']!r}",
            )
        entry = dict(em)
        entry["line_number"] = ln
        entry["file_path"] = file_path
        entry["language"] = lang
        if "line_content" not in entry:
            idx = ln - 1
            entry["line_content"] = (
                source_lines[idx] if 0 <= idx < len(source_lines) else ""
            )
        matches.append(entry)
    return matches, None


def _normalize_eval_result(
    eval_result: "EvalResult",
    file_path: str,
    source: str,
    lang: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Apply full result contract to an EvalResult; return (matches, errors, meta)."""
    if eval_result.failure == "evaluator_timeout":
        return _batch_error(
            file_path, "EvaluatorTimeout", "evaluator exceeded 5s sandbox limit"
        )
    if eval_result.failure is not None:
        return _batch_error(
            file_path, "EvaluatorCrash", eval_result.detail or eval_result.failure
        )

    raw_value = eval_result.value
    if not isinstance(raw_value, dict):
        return _batch_error(
            file_path,
            "InvalidEvaluatorReturn",
            f"Evaluator must return a dict, got {type(raw_value).__name__!r}",
        )
    if raw_value.get("skip") is True:
        return ([], [], None)
    if "matches" not in raw_value:
        return _batch_error(
            file_path,
            "InvalidEvaluatorReturn",
            "Evaluator dict missing required 'matches' key.",
        )
    evaluator_matches = raw_value["matches"]
    if not isinstance(evaluator_matches, list):
        return _batch_error(
            file_path,
            "InvalidEvaluatorReturn",
            f"'matches' must be a list, got {type(evaluator_matches).__name__!r}",
        )

    matches, err = _build_matches_from_evaluator(
        evaluator_matches, file_path, lang, source.splitlines()
    )
    if err is not None:
        return err

    per_file_value = raw_value.get("value", None)
    per_file_role = raw_value.get("file_role", None)
    file_meta: Optional[Dict[str, Any]] = None
    if per_file_value is not None or per_file_role is not None:
        file_meta = {"file_path": file_path}
        if per_file_value is not None:
            file_meta["value"] = per_file_value
        if per_file_role is not None:
            file_meta["file_role"] = per_file_role
    return matches, [], file_meta


def _process_one_file_in_driver(
    evaluator_code: str,
    spec: Dict[str, Any],
    ast_engine: Any,
    sandbox: "PythonEvaluatorSandbox",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Process a single file inside the driver process.

    Re-parses source with tree-sitter (XRayNode objects are not picklable),
    enriches match_positions with ast_node from byte offset, runs evaluator.
    """
    file_path = spec.get("file_path", "")
    source = spec.get("source", "")
    lang = spec.get("lang", "")
    raw_positions: List[Dict[str, Any]] = list(spec.get("match_positions") or [])

    try:
        source_bytes = source.encode("utf-8") if isinstance(source, str) else source
        root = ast_engine.parse(source_bytes, lang)

        positions: List[Dict[str, Any]] = []
        for pos in raw_positions:
            entry = dict(pos)
            ln = entry.get("line_number", 1) or 1
            entry["byte_offset"] = _line_to_byte_offset_bytes(source, ln)
            positions.append(entry)

        for pos in positions:
            byte_off = pos.get("byte_offset")
            pos["ast_node"] = (
                root.node_at_byte_offset(byte_off) if byte_off is not None else None
            )

        eval_result = sandbox.run(
            evaluator_code,
            node=root,
            root=root,
            source=source,
            lang=lang,
            file_path=file_path,
            match_positions=positions,
        )
        return _normalize_eval_result(eval_result, file_path, source, lang)
    except Exception as exc:  # noqa: BLE001
        return _batch_error(file_path, type(exc).__name__, str(exc))


def _driver_process(
    conn: Any,
    file_specs: List[Dict[str, Any]],
    evaluator_code: str,
    worker_threads: int,
) -> None:
    """Entry point for the spawned driver process.

    Module-level for picklability in the spawn multiprocessing context.
    Imports AstSearchEngine here (deferred import — tree-sitter not loaded at startup).
    """
    try:
        from code_indexer.xray.ast_engine import AstSearchEngine

        ast_engine = AstSearchEngine()
        sandbox = PythonEvaluatorSandbox()
        results: List[Any] = [None] * len(file_specs)

        def _process(idx_spec: Tuple[int, Dict[str, Any]]) -> Tuple[int, Any]:
            idx, spec = idx_spec
            return idx, _process_one_file_in_driver(
                evaluator_code, spec, ast_engine, sandbox
            )

        with ThreadPoolExecutor(max_workers=worker_threads) as pool:
            for idx, result in pool.map(_process, enumerate(file_specs)):
                results[idx] = result

        conn.send(results)
    except Exception as exc:  # noqa: BLE001
        conn.send(f"__driver_exception__:{type(exc).__name__}:{exc}")
    finally:
        conn.close()


def _collect_driver_result(
    parent_conn: Any,
    proc: Any,
    file_specs: List[Dict[str, Any]],
) -> Optional[List[Any]]:
    """Read batch results from driver pipe; return list or None on failure.

    Closes parent_conn exactly once in a finally block, reaps the process.
    Returns None if pipe has no data, EOF, or driver sent an exception string
    — caller converts None to per-file errors.

    NOTE: poll(timeout=5.0) not 0.0 — after proc.join() the kernel pipe buffer
    may not be readable with zero latency even though the child has already sent
    data and exited.  5 s is conservative; the process is already dead at this
    point so the only wait is for OS pipe-buffer propagation (sub-millisecond in
    practice).
    """
    try:
        has_data = parent_conn.poll(timeout=5.0)
        if not has_data:
            return None
        try:
            raw = parent_conn.recv()
        except EOFError:
            return None
        if isinstance(raw, str) and raw.startswith("__driver_exception__:"):
            return None
        # multiprocessing pipe recv() returns Any; actual payload is a list
        # serialized by the driver — the static type system cannot express this.
        return raw  # type: ignore[no-any-return]
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.join(timeout=0.0)


def _cancel_remaining(
    futures: Dict[Future, int],
    file_specs: List[Dict[str, Any]],
    results: List[
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
    ],
    pool: ThreadPoolExecutor,
) -> None:
    """Cancel all outstanding futures and shut down the pool without blocking.

    Checks ``fut.cancel()`` return value: returns True for futures that were
    not yet started (safely cancelled), False for futures already running
    (cannot be cancelled — they will run to completion in the background but
    their results are ignored).  Pre-filled timeout error entries in ``results``
    remain for any future whose result is not yet collected.
    """
    for fut, idx in futures.items():
        cancelled = fut.cancel()
        if not cancelled and not fut.done():
            # Already running — mark explicitly as timeout; result will be ignored.
            fp = file_specs[idx].get("file_path", "")
            results[idx] = _batch_error(
                fp, "EvaluatorTimeout", "batch inline exceeded timeout"
            )
    pool.shutdown(wait=False, cancel_futures=True)


def _run_inline_batch(
    sandbox: "PythonEvaluatorSandbox",
    file_specs: List[Dict[str, Any]],
    evaluator_code: str,
    worker_threads: int,
    timeout_seconds: int,
    ast_engine: Any,
) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """Process files inline in the caller's process using a ThreadPoolExecutor.

    Calls ``ast_engine.parse()`` and ``sandbox.run()`` per file in the parent
    process, so patches on either (e.g. in tests) are properly intercepted.

    Input validation (worker_threads >= 1, timeout_seconds > 0) is performed
    by the calling ``run_batch()`` method before this function is invoked.

    Enforces ``timeout_seconds`` as a wall-clock cap: each future's result is
    collected with a per-item remaining-time budget.  On deadline, outstanding
    futures are cancelled via ``_cancel_remaining`` (which checks the cancel()
    return value to distinguish not-yet-started from already-running futures)
    and the pool is shut down immediately without blocking.
    """
    deadline = time.monotonic() + timeout_seconds
    # Pre-fill with timeout errors; overwritten as results arrive.
    results: List[
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
    ] = [
        _batch_error(
            spec.get("file_path", ""),
            "EvaluatorTimeout",
            "batch inline exceeded timeout",
        )
        for spec in file_specs
    ]

    def _process_one(
        idx: int,
        spec: Dict[str, Any],
    ) -> Tuple[
        int, Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]
    ]:
        return idx, _process_one_file_in_driver(
            evaluator_code, spec, ast_engine, sandbox
        )

    pool = ThreadPoolExecutor(max_workers=worker_threads)
    futures: Dict[Future, int] = {
        pool.submit(_process_one, idx, spec): idx for idx, spec in enumerate(file_specs)
    }

    for fut, idx in futures.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _cancel_remaining(futures, file_specs, results, pool)
            return results
        try:
            _, file_result = fut.result(timeout=remaining)
            results[idx] = file_result
        except TimeoutError:
            _cancel_remaining(futures, file_specs, results, pool)
            return results
        except Exception as exc:  # noqa: BLE001
            # Broad catch is required: any error from parse(), ast_engine, or
            # sandbox.run() must produce a per-file error entry rather than
            # crashing the entire batch. The _process_one_file_in_driver helper
            # already catches its own exceptions, so this guard catches only
            # unexpected failures in the Future machinery itself.
            fp = file_specs[idx].get("file_path", "")
            results[idx] = _batch_error(fp, type(exc).__name__, str(exc))

    pool.shutdown(wait=False, cancel_futures=True)
    return results


def _run_driver_batch(
    sandbox: "PythonEvaluatorSandbox",
    file_specs: List[Dict[str, Any]],
    evaluator_code: str,
    worker_threads: int,
    timeout_seconds: int,
    on_process_spawned: Optional[Callable] = None,
) -> List[Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """Spawn ONE driver and collect batch results; convert all failures to per-file errors."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)

    proc = ctx.Process(
        target=_driver_process,
        args=(child_conn, file_specs, evaluator_code, worker_threads),
        daemon=False,
    )
    proc.start()
    if on_process_spawned is not None:
        on_process_spawned(proc)
    child_conn.close()
    proc.join(timeout=timeout_seconds)

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=sandbox.SIGKILL_GRACE_SECONDS)
        if proc.is_alive():
            proc.kill()
            proc.join()
        parent_conn.close()
        return [
            _batch_error(
                s.get("file_path", ""),
                "EvaluatorTimeout",
                "batch driver exceeded timeout",
            )
            for s in file_specs
        ]

    results = _collect_driver_result(parent_conn, proc, file_specs)
    if results is None:
        return [
            _batch_error(
                s.get("file_path", ""),
                "EvaluatorCrash",
                f"driver exitcode={proc.exitcode}",
            )
            for s in file_specs
        ]
    return results


# ---------------------------------------------------------------------------
# Rust evaluator pre-flight validator
# ---------------------------------------------------------------------------

# Ordered table of (error_code, offending_construct, regex_pattern) entries.
# Entries are checked in order; the first match short-circuits.
# Patterns are case-sensitive except where noted.
_RUST_FORBIDDEN_PATTERNS: list[tuple[str, str, str]] = [
    # Unsafe code — top priority
    ("forbidden_unsafe", "unsafe", r"\bunsafe\b"),
    # Forbidden std namespaces
    ("forbidden_stdlib", "std::fs", r"\bstd::fs\b"),
    ("forbidden_stdlib", "std::net", r"\bstd::net\b"),
    ("forbidden_stdlib", "std::process", r"\bstd::process\b"),
    ("forbidden_stdlib", "std::env", r"\bstd::env\b"),
    ("forbidden_stdlib", "std::io", r"\bstd::io\b"),
    # Raw pointer types
    ("forbidden_raw_ptr", "*const", r"\*const\b"),
    ("forbidden_raw_ptr", "*mut", r"\*mut\b"),
    # Structural declarations
    ("forbidden_extern", "extern", r"\bextern\b"),
    ("forbidden_mod", "mod", r"\bmod\b"),
    ("forbidden_static_mut", "static mut", r"\bstatic\s+mut\b"),
    # Forbidden macros (macro invocation: name followed by !)
    ("forbidden_macro", "include!", r"\binclude\s*!"),
    ("forbidden_macro", "env!", r"\benv\s*!"),
    ("forbidden_macro", "println!", r"\bprintln\s*!"),
    ("forbidden_macro", "eprintln!", r"\beprintln\s*!"),
    ("forbidden_macro", "panic!", r"\bpanic\s*!"),
    ("forbidden_macro", "todo!", r"\btodo\s*!"),
    ("forbidden_macro", "unimplemented!", r"\bunimplemented\s*!"),
    ("forbidden_macro", "include_str!", r"\binclude_str\s*!"),
    ("forbidden_macro", "include_bytes!", r"\binclude_bytes\s*!"),
    ("forbidden_macro", "option_env!", r"\boption_env\s*!"),
    ("forbidden_macro", "print!", r"\bprint\s*!"),
    ("forbidden_macro", "eprint!", r"\beprint\s*!"),
]

# Pre-compiled pattern objects for performance.
_RUST_COMPILED_PATTERNS: list[tuple[str, str, "_re.Pattern[str]"]] = [
    (error_code, construct, _re.compile(pattern))
    for error_code, construct, pattern in _RUST_FORBIDDEN_PATTERNS
]


def validate_rust_evaluator(code: str) -> ValidationResult:
    """Statically validate Rust evaluator code for required signature and forbidden constructs.

    Checks that the code contains ``fn evaluate_node`` and rejects any
    forbidden Rust construct (unsafe, dangerous std namespaces, raw pointers,
    extern blocks, mod declarations, static mut, forbidden macros).

    Returns:
        ValidationResult with ``ok=True`` when the code is acceptable, or
        ``ok=False`` with ``error_code``, ``reason``, ``offending_construct``,
        and ``offending_line`` describing the first violation found.
    """
    if not _re.search(r"\bfn\s+evaluate_node\b", code):
        return ValidationResult(
            ok=False,
            reason="missing required 'fn evaluate_node' function signature",
            error_code="missing_entry_point",
            offending_construct="evaluate_node",
            offending_line=None,
        )

    for lineno, line in enumerate(code.splitlines(), start=1):
        for error_code, construct, pattern in _RUST_COMPILED_PATTERNS:
            if pattern.search(line):
                return ValidationResult(
                    ok=False,
                    reason=f"forbidden construct '{construct}' is not allowed in Rust evaluator code",
                    error_code=error_code,
                    offending_construct=construct,
                    offending_line=lineno,
                )

    return ValidationResult(ok=True)
