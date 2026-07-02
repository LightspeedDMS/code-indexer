"""Regression tests for the v10.4.3 dunder-in-Slice bypass fix.

With ast.Slice now in the ALLOWED_NODES whitelist, dunder strings could
previously hide inside Slice.lower/upper/step and pass validation:

    return obj['__class__':10]   -> ast.Subscript(slice=ast.Slice(lower=Constant('__class__')))

The new check in validate() catches all three Slice component positions.

Each test calls PythonEvaluatorSandbox().validate() directly — no subprocess
is spawned for validation-only tests.
"""

from __future__ import annotations

from code_indexer.xray.sandbox import PythonEvaluatorSandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate(code: str):
    return PythonEvaluatorSandbox().validate(code)


def _assert_blocked(code: str, expected_fragment: str) -> None:
    result = _validate(code)
    assert result.ok is False, f"Expected rejection but got ok=True for: {code!r}"
    assert expected_fragment in result.reason, (
        f"Expected {expected_fragment!r} in reason {result.reason!r} for code: {code!r}"
    )


def _assert_allowed(code: str) -> None:
    result = _validate(code)
    assert result.ok is True, (
        f"Expected ok=True but got rejection for: {code!r}\nReason: {result.reason}"
    )


# ---------------------------------------------------------------------------
# Tests: dunder strings in Slice positions must be blocked
# ---------------------------------------------------------------------------


def test_slice_lower_dunder_blocked():
    """Dunder string in Slice.lower position is blocked (obj['__class__':10])."""
    _assert_blocked("return obj['__class__':10]", "'__class__'")
    _assert_blocked("return obj['__class__':10]", "blocked")


def test_slice_upper_dunder_blocked():
    """Dunder string in Slice.upper position is blocked (obj[10:'__globals__'])."""
    _assert_blocked("return obj[10:'__globals__']", "'__globals__'")
    _assert_blocked("return obj[10:'__globals__']", "blocked")


def test_slice_step_dunder_blocked():
    """Dunder string in Slice.step position is blocked (obj[::'__subclasses__'])."""
    _assert_blocked("return obj[::'__subclasses__']", "'__subclasses__'")
    _assert_blocked("return obj[::'__subclasses__']", "blocked")


def test_slice_open_upper_dunder_blocked():
    """Dunder string in Slice.upper with no lower (obj[:'__import__']) is blocked.

    In obj[:'__import__']: lower=None, upper='__import__' — dunder is in Slice.upper.
    """
    _assert_blocked("return obj[:'__import__']", "'__import__'")
    _assert_blocked("return obj[:'__import__']", "blocked")


def test_slice_open_lower_dunder_blocked():
    """Dunder string in Slice.lower with no upper/step (obj['__builtins__'::]) is blocked.

    In obj['__builtins__'::]: lower='__builtins__', upper=None, step=None — dunder is in Slice.lower.
    """
    _assert_blocked("return obj['__builtins__'::]", "'__builtins__'")
    _assert_blocked("return obj['__builtins__'::]", "blocked")


# ---------------------------------------------------------------------------
# Tests: legitimate slices must remain allowed
# ---------------------------------------------------------------------------


def test_slice_legit_integer_slice_still_allowed():
    """Integer slice indices do not contain dunder strings — must be allowed."""
    _assert_allowed("return source[10:20]")
    _assert_allowed("return source[-30:]")
    _assert_allowed("return lines[0:10:2]")


def test_slice_with_variable_components_allowed():
    """Variable Name nodes in slice components are not Constant strings — allowed."""
    _assert_allowed("return source[start:end]")


# ---------------------------------------------------------------------------
# Regression guard: original direct-Constant dunder block still works
# ---------------------------------------------------------------------------


def test_dunder_in_subscript_constant_still_blocked_regression():
    """The original obj['__class__'] (direct Constant, no Slice) is still blocked.

    This is a regression guard: the new Slice check must not shadow or remove
    the existing Constant-subscript check that was already in place.
    """
    result = _validate("return obj['__class__']")
    assert result.ok is False, (
        "Regression: obj['__class__'] (direct Constant subscript) must still be blocked"
    )
    assert "'__class__'" in result.reason or "__class__" in result.reason, (
        f"Expected '__class__' in reason {result.reason!r}"
    )
    assert "blocked" in result.reason, f"Expected 'blocked' in reason {result.reason!r}"
