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

    - ``failure is None`` — success; ``value`` holds the bool result.
    - ``failure == "validation_failed"`` — static analysis rejected the code;
      ``detail`` contains the rejection reason.
    - ``failure == "evaluator_timeout"`` — subprocess did not finish within
      ``HARD_TIMEOUT_SECONDS``; it was terminated.
    - ``failure == "evaluator_subprocess_died"`` — subprocess exited with
      non-zero code (segfault, NameError on stripped builtin, etc.);
      ``detail`` carries ``exitcode=N`` or ``no_pipe_data``.
    - ``failure == "evaluator_returned_non_bool"`` — subprocess exited cleanly
      but sent a value that is not a ``bool``; ``detail`` is the type name.
    """

    value: Optional[bool] = field(default=None)
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
) -> None:
    """Execute *code* inside a stripped-builtin environment and send result via *conn*.

    This function runs inside a forked/spawned subprocess.  All imports needed
    here must be available in the child process (they are, via fork inheritance).
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
        }
        locals_dict: dict[str, Any] = {}

        # Wrap user code inside a function so ``return`` works at the top level
        wrapped = "def __evaluator__():\n" + textwrap.indent(code, "    ")
        exec(wrapped, globals_dict, locals_dict)  # noqa: S102
        result = locals_dict["__evaluator__"]()
        conn.send(bool(result))
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
    # (ast.boolop, ast.cmpop, ast.unaryop, ast.expr_context) correctly
    # match their concrete subclasses (Eq, And, Not, Load, etc.).
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
                    reason=f"{type(node).__name__} not in allowed nodes",
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
    ) -> EvalResult:
        """Run *code* in a sandboxed subprocess and return the result.

        Validates *code* first — if validation fails the subprocess is never
        spawned.  On success the subprocess receives SIGTERM after
        ``HARD_TIMEOUT_SECONDS`` and SIGKILL after an additional
        ``SIGKILL_GRACE_SECONDS`` if it has not already exited.

        Args:
            code: Evaluator source code (must pass ``validate()``).
            node: Current AST node (XRayNode) passed to the evaluator.
            root: Root AST node (XRayNode) passed to the evaluator.
            source: Full source text of the file being evaluated.
            lang: Language identifier string (e.g. ``"java"``, ``"python"``).
            file_path: Absolute path of the file being evaluated.

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
            args=(code, node, root, source, lang, file_path, child_conn),
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

        if not isinstance(raw, bool):
            return EvalResult(
                failure="evaluator_returned_non_bool",
                detail=type(raw).__name__,
            )

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
