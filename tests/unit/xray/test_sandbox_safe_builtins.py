"""User Mandate Section 4: Safe Builtins Allowlist — Positive Tests (Story #970).

Only the 8 transpilable builtins are allowed: len, any, all, range, enumerate,
sorted, min, max.  All other builtins were removed (Rust-only path).

  1. Construct evaluator code that uses the builtin and returns a bool.
  2. Assert the evaluator runs successfully (no failure mode).
  3. Assert the return value matches the expected bool.

These tests prevent accidental over-stripping that would break legitimate
evaluator code by removing builtins that safe evaluators rely on.
"""

from __future__ import annotations

import pytest

from code_indexer.xray.ast_engine import AstSearchEngine
from code_indexer.xray.sandbox import (
    EvalResult,
    PythonEvaluatorSandbox,
    SAFE_BUILTIN_NAMES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_root(source: str = "x = 1", lang: str = "python"):
    engine = AstSearchEngine()
    root = engine.parse(source, lang)
    return root, root


def _run_safe(code: str) -> EvalResult:
    """Run evaluator code expected to succeed cleanly."""
    sb = PythonEvaluatorSandbox()
    node, root = _make_node_root()
    return sb.run(
        code,
        node=node,
        root=root,
        source="x = 1",
        lang="python",
        file_path="/src/main.py",
    )


# ---------------------------------------------------------------------------
# Section 4: Safe builtins allowlist — parametrized positive tests
# ---------------------------------------------------------------------------

# Each tuple: (builtin_name, evaluator_code, expected_bool)
# The code must use the builtin and return a predictable bool.
# NOTE: only the 8 allowed builtins (len, any, all, range, enumerate, sorted,
# min, max) may be used — list(), str(), int() etc. are no longer safe.
_SAFE_BUILTIN_CASES = [
    (
        "len",
        "return len([1, 2, 3]) > 0",
        True,
    ),
    (
        "min",
        "return min(3, 1, 2) == 1",
        True,
    ),
    (
        "max",
        "return max(3, 1, 2) == 3",
        True,
    ),
    (
        "any",
        "return any([False, True, False]) is True",
        True,
    ),
    (
        "all",
        "return all([True, True, True]) is True",
        True,
    ),
    (
        "range",
        # Use len() + a for-loop counter instead of list(range(...))
        "count = 0\nfor _ in range(5):\n    count += 1\nreturn count == 5",
        True,
    ),
    (
        "enumerate",
        # Iterate with enumerate; check first index via assignment, no list()
        "first_idx = -1\nfor idx, _ in enumerate([10, 20]):\n    if first_idx == -1:\n        first_idx = idx\nreturn first_idx == 0",
        True,
    ),
    (
        "sorted",
        "return sorted([3, 1, 2]) == [1, 2, 3]",
        True,
    ),
]


@pytest.mark.parametrize("builtin_name,code,expected", _SAFE_BUILTIN_CASES)
def test_safe_builtin_available_and_returns_expected(
    builtin_name: str, code: str, expected: bool
) -> None:
    """Safe builtin is available in the evaluator; code runs and returns expected bool."""
    result = _run_safe(code)
    assert result.failure is None, (
        f"Safe builtin '{builtin_name}' caused evaluator failure: "
        f"failure={result.failure!r}, detail={result.detail!r}"
    )
    assert result.value == expected, (
        f"Safe builtin '{builtin_name}' returned {result.value!r}, "
        f"expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# Meta: verify SAFE_BUILTIN_NAMES matches the spec
# ---------------------------------------------------------------------------


def test_safe_builtin_names_set_matches_spec() -> None:
    """Verify SAFE_BUILTIN_NAMES contains exactly the 8 transpilable builtins."""
    expected = frozenset(
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
    assert SAFE_BUILTIN_NAMES == expected, (
        f"SAFE_BUILTIN_NAMES mismatch. "
        f"Extra: {SAFE_BUILTIN_NAMES - expected}, "
        f"Missing: {expected - SAFE_BUILTIN_NAMES}"
    )


def test_safe_and_stripped_builtins_are_disjoint() -> None:
    """No builtin should appear in both the safe list and the stripped list."""
    overlap = SAFE_BUILTIN_NAMES & PythonEvaluatorSandbox.STRIPPED_BUILTINS
    assert not overlap, f"Builtins in both safe and stripped sets: {overlap}"


def test_safe_builtin_names_count() -> None:
    """SAFE_BUILTIN_NAMES must contain exactly 8 entries (Rust-transpilable only)."""
    assert len(SAFE_BUILTIN_NAMES) == 8, (
        f"Expected 8 entries in SAFE_BUILTIN_NAMES, got {len(SAFE_BUILTIN_NAMES)}: "
        f"{sorted(SAFE_BUILTIN_NAMES)}"
    )
