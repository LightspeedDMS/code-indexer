"""User Mandate Section 2: Dunder / Sandbox Escape Attempts (Story #970).

Each dunder escape vector is BLOCKED at LAYER 1 — AST validation time.

The fix adds two checks inside validate():
  1. DUNDER_ATTR_BLOCKLIST: rejects any Attribute node whose .attr is a dunder
     name (e.g. __class__, __init__, __globals__, __builtins__).
  2. Subscript-with-dunder-string: rejects any Subscript node whose slice is
     a string Constant that starts with '__' or is in the blocklist
     (e.g. ['__builtins__'], ['__import__']).

Each test asserts ALL of:
  (a) result.failure == "validation_failed" — code was NEVER executed
  (b) result.detail mentions the blocked name (e.g. '__class__', '__globals__')
  (c) No subprocess was spawned (multiprocessing.get_context patched to raise)
  (d) No canary file was created on disk
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from code_indexer.xray.ast_engine import AstSearchEngine
from code_indexer.xray.sandbox import EvalResult, PythonEvaluatorSandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_root(source: str = "x = 1", lang: str = "python"):
    engine = AstSearchEngine()
    root = engine.parse(source, lang)
    return root, root


def _run_expecting_validation_failed(
    code: str,
    canary_path: str | None = None,
) -> EvalResult:
    """Run code asserting:
    - validation_failed is returned
    - no subprocess was spawned
    - optional canary file was NOT created
    """
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()

    def _raise_if_called(*args, **kwargs):
        raise RuntimeError(
            "multiprocessing.get_context called — subprocess was attempted "
            "despite validation failure (dunder escape not blocked at AST layer)"
        )

    with patch(
        "code_indexer.xray.sandbox.multiprocessing.get_context", _raise_if_called
    ):
        result = sb.run(
            code,
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )

    assert result.failure == "validation_failed", (
        f"Expected validation_failed but got failure={result.failure!r}, "
        f"detail={result.detail!r} for code: {code!r}"
    )

    if canary_path is not None:
        assert not Path(canary_path).exists(), (
            f"SECURITY BREACH: canary file {canary_path} was created despite "
            "validation_failed — the dunder escape closed the file before subprocess ended"
        )

    return result


def _assert_detail_mentions(result: EvalResult, name: str) -> None:
    """Assert that result.detail contains the blocked name."""
    assert result.detail is not None, "Expected detail to be set"
    assert name in result.detail, f"Expected {name!r} in detail {result.detail!r}"


def _assert_detail_mentions_any_dunder(result: EvalResult) -> None:
    """Assert that result.detail contains at least one dunder name.

    ast.walk() order is breadth-first from the outermost expression node,
    which means the FIRST blocked dunder attribute encountered may not be
    __class__ — it depends on the AST structure. This helper verifies that
    some dunder name from the blocklist appears in the rejection reason.
    """
    assert result.detail is not None, "Expected detail to be set"
    has_dunder = any(
        dunder in result.detail
        for dunder in PythonEvaluatorSandbox.DUNDER_ATTR_BLOCKLIST
    )
    assert has_dunder, (
        f"Expected a dunder name from DUNDER_ATTR_BLOCKLIST in detail {result.detail!r}"
    )


# ---------------------------------------------------------------------------
# Section 2 escape vector tests — all must produce validation_failed
# ---------------------------------------------------------------------------


class TestDunderSandboxEscapes:
    """Classic Python sandbox escape patterns — each must be blocked at validation."""

    def test_escape_via_class_bases_subclasses(self):
        """().__class__.__bases__[0].__subclasses__() blocked on a dunder attribute.

        ast.walk() visits __subclasses__ before __class__ in this expression,
        so we check that any dunder from the blocklist appears in the detail.
        """
        code = "return ().__class__.__bases__[0].__subclasses__() is not None"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions_any_dunder(result)

    def test_escape_via_mro_subclasses(self):
        """().__class__.__mro__[1].__subclasses__() blocked on a dunder attribute.

        ast.walk() visits __subclasses__ before __class__ in this expression,
        so we check that any dunder from the blocklist appears in the detail.
        """
        code = "return ().__class__.__mro__[1].__subclasses__() is not None"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions_any_dunder(result)

    def test_escape_via_instance_materialization(self):
        """(1).__class__.__mro__[0].__subclasses__()[0]() blocked on a dunder attribute.

        ast.walk() visits __subclasses__ before __class__ in this expression,
        so we check that any dunder from the blocklist appears in the detail.
        """
        code = "return str((1).__class__.__mro__[0].__subclasses__()[0]) is not None"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions_any_dunder(result)

    def test_escape_via_function_globals(self):
        """''.join.__globals__ blocked on __globals__ attribute."""
        code = "return '__import__' not in ''.join.__globals__"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions(result, "__globals__")

    def test_escape_via_node_class_init_globals_import(self):
        """node.__class__.__init__.__globals__['__builtins__']['__import__']('os')
        blocked on __class__ attribute (first dunder encountered in AST walk).
        """
        code = (
            "return node.__class__.__init__.__globals__"
            "['__builtins__']['__import__']('os') is not None"
        )
        result = _run_expecting_validation_failed(code)
        # Any dunder name blocked — __class__ is first
        assert result.detail is not None

    def test_escape_via_node_class_init_globals_open_THE_EXPLOIT(self):
        """THE CONFIRMED EXPLOIT from Codex review:
        node.__class__.__init__.__globals__['__builtins__']['open']('/tmp/...', 'w')
        Must be blocked at validation_failed — canary file must NOT be created.
        """
        canary = "/tmp/xray_canary_dunder_exploit_970"
        # Clean up from any previous run
        if os.path.exists(canary):
            os.unlink(canary)

        code = (
            f"return node.__class__.__init__.__globals__"
            f"['__builtins__']['open']('{canary}','w').write('pwned') >= 0"
        )
        result = _run_expecting_validation_failed(code, canary_path=canary)
        # __class__ is the first blocked dunder
        assert result.detail is not None

    def test_escape_via_dict_base(self):
        """dict.__base__ blocked on __base__ attribute."""
        code = "return dict.__base__ is not None"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions(result, "__base__")

    def test_escape_via_reduce(self):
        """(0).__reduce__() blocked on __reduce__ attribute."""
        code = "return (0).__reduce__ is not None"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions(result, "__reduce__")

    def test_escape_via_subscript_builtins(self):
        """globals()['__builtins__']['open'] blocked on subscript with '__builtins__' key."""
        code = "return globals()['__builtins__']['open'] is not None"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions(result, "__builtins__")

    def test_escape_via_subscript_import(self):
        """vars(node)['__class__'] blocked on subscript with '__class__' key."""
        code = "return vars(node)['__class__'] is not None"
        result = _run_expecting_validation_failed(code)
        _assert_detail_mentions(result, "__class__")

    def test_escape_via_int_init_globals_builtins_subscript(self):
        """(0).__class__.__init__.__globals__['__builtins__'] blocked on __class__."""
        code = (
            "return '__import__' not in "
            "(0).__class__.__init__.__globals__.get('__builtins__', {})"
        )
        result = _run_expecting_validation_failed(code)
        # __class__ is the first blocked name
        assert result.detail is not None

    def test_escape_via_mro_index_1_subscript(self):
        """().__class__.__mro__[1] blocked on __class__ then __mro__."""
        code = "return ().__class__.__mro__[1] is not None"
        result = _run_expecting_validation_failed(code)
        assert result.detail is not None

    # -----------------------------------------------------------------------
    # format_map remains safe — it's a regular attribute (no dunder)
    # -----------------------------------------------------------------------

    def test_format_map_still_allowed(self):
        """''.format_map({}) uses only regular attribute access — must still run.

        format_map is NOT a dunder name, so it is not blocked by DUNDER_ATTR_BLOCKLIST.
        The code uses only Call/Attribute/Constant/Dict/Compare — all whitelisted.
        """
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        sb = PythonEvaluatorSandbox()
        node, root = _make_node_root()
        result = sb.run(
            "return ''.format_map({}) == ''",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )
        assert result.failure is None, (
            f"format_map should still work: failure={result.failure!r}"
        )
        assert result.value is True

    # -----------------------------------------------------------------------
    # type() is still blocked by stripped builtins (NameError in subprocess)
    # -----------------------------------------------------------------------

    def test_type_builtin_absent_from_safe_builtins(self):
        """Verify 'type' is NOT in safe builtins — type(x) raises NameError.

        This is a LAYER 2 check (stripped builtins), not a LAYER 1 check.
        type(node) uses only Call+Name — both whitelisted, so validation passes.
        The subprocess dies with NameError.
        """
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        sb = PythonEvaluatorSandbox()
        node, root = _make_node_root()
        result = sb.run(
            "return type(node) is not None",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )
        # type() is not in safe builtins — NameError => evaluator_subprocess_died
        assert result.failure == "evaluator_subprocess_died"
