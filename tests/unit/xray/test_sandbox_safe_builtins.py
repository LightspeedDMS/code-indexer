"""User Mandate Section 4: Safe Builtins Allowlist — Positive Tests (Story #970).

For each of the 18 safe builtins (len, str, int, bool, list, tuple, dict,
min, max, sum, any, all, range, enumerate, zip, sorted, reversed, hasattr):
(hasattr added in M1 Codex review finding)

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
_SAFE_BUILTIN_CASES = [
    (
        "len",
        "return len([1, 2, 3]) > 0",
        True,
    ),
    (
        "str",
        "return str(42) == '42'",
        True,
    ),
    (
        "int",
        "return int('7') == 7",
        True,
    ),
    (
        "bool",
        "return bool(1) is True",
        True,
    ),
    (
        "list",
        "return list((1, 2)) == [1, 2]",
        True,
    ),
    (
        "tuple",
        "return tuple([1, 2]) == (1, 2)",
        True,
    ),
    (
        "dict",
        "return dict([('a', 1)]).get('a') == 1",
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
        "sum",
        "return sum([1, 2, 3]) == 6",
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
        "return len(list(range(5))) == 5",
        True,
    ),
    (
        "enumerate",
        "return list(enumerate([10, 20]))[0] == (0, 10)",
        True,
    ),
    (
        "zip",
        "return list(zip([1, 2], [3, 4])) == [(1, 3), (2, 4)]",
        True,
    ),
    (
        "sorted",
        "return sorted([3, 1, 2]) == [1, 2, 3]",
        True,
    ),
    (
        "reversed",
        "return list(reversed([1, 2, 3])) == [3, 2, 1]",
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
    """Verify SAFE_BUILTIN_NAMES contains exactly the 18 specified builtins (hasattr added in M1)."""
    expected = frozenset(
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
            "hasattr",  # moved from STRIPPED_BUILTINS in M1 Codex review finding
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


# ---------------------------------------------------------------------------
# M1: hasattr moved from STRIPPED_BUILTINS to SAFE_BUILTIN_NAMES
# ---------------------------------------------------------------------------


def test_hasattr_moved_to_safe_builtins() -> None:
    """hasattr must be in SAFE_BUILTIN_NAMES and NOT in STRIPPED_BUILTINS (M1)."""
    assert "hasattr" in SAFE_BUILTIN_NAMES, (
        "hasattr must be in SAFE_BUILTIN_NAMES — it has no escalation power "
        "beyond what the dunder blocklist already prevents at AST validation time."
    )
    assert "hasattr" not in PythonEvaluatorSandbox.STRIPPED_BUILTINS, (
        "hasattr must NOT remain in STRIPPED_BUILTINS after M1 migration."
    )


def test_hasattr_works_in_evaluator() -> None:
    """hasattr(node, 'children') succeeds in the evaluator after M1 (M1)."""
    sb = PythonEvaluatorSandbox()
    engine = AstSearchEngine()
    node = engine.parse("x = 1", "python")
    result = sb.run(
        "return hasattr(node, 'children')",
        node=node,
        root=node,
        source="x = 1",
        lang="python",
        file_path="/tmp/x.py",
    )
    assert result.failure is None, (
        f"hasattr must succeed in evaluator, got failure={result.failure!r}, "
        f"detail={result.detail!r}"
    )
    assert result.value is True, (
        f"hasattr(node, 'children') must return True, got {result.value!r}"
    )


def test_safe_builtin_names_set_has_18_entries() -> None:
    """After M1, SAFE_BUILTIN_NAMES must contain exactly 18 builtins (17 + hasattr)."""
    expected = frozenset(
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
            "hasattr",
        }
    )
    assert SAFE_BUILTIN_NAMES == expected, (
        f"SAFE_BUILTIN_NAMES mismatch after M1. "
        f"Extra: {SAFE_BUILTIN_NAMES - expected}, "
        f"Missing: {expected - SAFE_BUILTIN_NAMES}"
    )
