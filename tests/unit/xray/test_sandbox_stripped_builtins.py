"""User Mandate Section 3: Stripped Builtins — One Test Per Builtin (Story #970).

hasattr was moved to SAFE_BUILTIN_NAMES in M1 (Codex review finding).
For each of the remaining 8 stripped builtins (getattr, setattr, delattr,
__import__, eval, exec, open, compile):

  1. Evaluator code calls the builtin as Call(Name(builtin_name), ...) which
     IS a whitelisted AST node type — so validate() must PASS.
  2. The subprocess raises NameError because the builtin is absent from the
     exec() environment.
  3. The parent receives EvalResult(failure="evaluator_subprocess_died").
  4. No side effects: no file created, no orphan child process.

For ``open``: also assert the canary file does NOT exist after the test.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from code_indexer.xray.ast_engine import AstSearchEngine
from code_indexer.xray.sandbox import EvalResult, PythonEvaluatorSandbox

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node_root(source: str = "x = 1", lang: str = "python"):
    engine = AstSearchEngine()
    root = engine.parse(source, lang)
    return root, root


def _run_stripped(code: str) -> EvalResult:
    """Run evaluator code expecting stripped-builtin NameError behavior."""
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


def _validate_passes(code: str) -> None:
    """Assert that validate() returns ok=True for the given code."""
    sb = PythonEvaluatorSandbox()
    result = sb.validate(code)
    assert result.ok is True, (
        f"Expected validate() to pass for code {code!r} but got: {result.reason}"
    )


# ---------------------------------------------------------------------------
# Section 3: Stripped builtins — parametrized for the 8 non-open builtins
# ---------------------------------------------------------------------------

# Each tuple: (builtin_name, evaluator_code_template)
# The code must be valid AST (only whitelisted nodes) so that validate() passes
# but the stripped builtin causes NameError at runtime.
_STRIPPED_BUILTIN_CASES = [
    (
        "getattr",
        "return getattr(node, 'type') is not None",
    ),
    (
        "setattr",
        "return setattr(node, 'fake_attr', 1) is None",
    ),
    (
        "delattr",
        "return delattr(node, 'fake_attr') is None",
    ),
    # hasattr was moved to SAFE_BUILTIN_NAMES in M1 — it is no longer stripped.
    # See test_sandbox_safe_builtins.py::test_hasattr_moved_to_safe_builtins.
    (
        "__import__",
        "return __import__('os') is not None",
    ),
    (
        "eval",
        "return eval('1 + 1') == 2",
    ),
    (
        "exec",
        "return exec('x = 1') is None",
    ),
    (
        "compile",
        "return compile('1+1', '<string>', 'eval') is not None",
    ),
]


@pytest.mark.parametrize("builtin_name,code", _STRIPPED_BUILTIN_CASES)
def test_stripped_builtin_causes_subprocess_died(builtin_name: str, code: str) -> None:
    """validate() passes (Call(Name) is whitelisted); subprocess dies with NameError."""
    # Step 1: validation must PASS — calling a Name is whitelisted
    _validate_passes(code)

    # Step 2: subprocess must fail because the builtin is absent
    result = _run_stripped(code)
    assert result.failure == "evaluator_subprocess_died", (
        f"Expected evaluator_subprocess_died for stripped builtin '{builtin_name}', "
        f"got: failure={result.failure!r}, detail={result.detail!r}"
    )

    # Step 3: detail must reference NameError AND mention the builtin name
    assert result.detail is not None, (
        f"Expected detail to be set for stripped builtin '{builtin_name}'"
    )
    assert "NameError" in result.detail, (
        f"Expected 'NameError' in detail for stripped builtin '{builtin_name}', "
        f"got: {result.detail!r}"
    )
    assert builtin_name in result.detail, (
        f"Expected builtin name '{builtin_name}' in detail, got: {result.detail!r}"
    )


# ---------------------------------------------------------------------------
# open — special case: canary file must NOT be created
# ---------------------------------------------------------------------------


def test_stripped_builtin_open_canary_file_not_created() -> None:
    """open() is stripped; the canary file must NOT be created by the evaluator."""
    canary = f"/tmp/xray_sandbox_canary_{uuid.uuid4().hex}"
    code = f"return open('{canary}', 'w') is not None"

    # validate() must pass — Call(Name('open'), ...) is whitelisted
    _validate_passes(code)

    try:
        result = _run_stripped(code)
        # Subprocess must die (NameError on 'open')
        assert result.failure == "evaluator_subprocess_died", (
            f"Expected evaluator_subprocess_died for open(), "
            f"got: failure={result.failure!r}, detail={result.detail!r}"
        )
    finally:
        # The canary file must NOT exist
        assert not Path(canary).exists(), (
            f"SECURITY BREACH: canary file {canary} was created by the evaluator "
            "despite 'open' being stripped from builtins"
        )


# ---------------------------------------------------------------------------
# Confirm: stripped builtins are absent from SAFE_BUILTIN_NAMES
# ---------------------------------------------------------------------------


def test_stripped_import_does_not_pollute_sys_modules() -> None:
    """Stripped __import__ must not add any new module to sys.modules even if invoked."""
    import sys

    # Choose a module unlikely to already be imported in the test process
    canary_module = "json.encoder"
    # Ensure canary is not already loaded before we snapshot
    if canary_module in sys.modules:
        del sys.modules[canary_module]

    before = set(sys.modules.keys())

    sb = PythonEvaluatorSandbox()
    engine = AstSearchEngine()
    node = engine.parse("x=1", "python")
    # __import__ as a Call node IS whitelisted; the lookup will fail with NameError
    # because __import__ is stripped from the exec env.
    result = sb.run(
        "return __import__('os.path') is None",
        node=node,
        root=node,
        source="x=1",
        lang="python",
        file_path="/tmp/x.py",
    )

    after = set(sys.modules.keys())
    new_modules = after - before

    assert result.failure == "evaluator_subprocess_died", (
        f"Expected evaluator_subprocess_died, got {result.failure!r}"
    )
    assert "NameError" in (result.detail or ""), (
        f"Expected NameError in detail, got {result.detail!r}"
    )
    assert not new_modules, (
        f"sys.modules polluted by stripped __import__: {new_modules}"
    )


def test_stripped_builtins_not_in_safe_builtin_names() -> None:
    """Verify every stripped builtin is absent from SAFE_BUILTIN_NAMES."""
    from code_indexer.xray.sandbox import SAFE_BUILTIN_NAMES

    for name in PythonEvaluatorSandbox.STRIPPED_BUILTINS:
        assert name not in SAFE_BUILTIN_NAMES, (
            f"Stripped builtin '{name}' must not appear in SAFE_BUILTIN_NAMES"
        )


def test_stripped_builtins_set_matches_spec() -> None:
    """Verify the 8 stripped builtins match the spec exactly (hasattr moved to safe in M1)."""
    expected = frozenset(
        {
            "getattr",
            "setattr",
            "delattr",
            # hasattr moved to SAFE_BUILTIN_NAMES (M1 Codex review finding)
            "__import__",
            "eval",
            "exec",
            "open",
            "compile",
        }
    )
    assert PythonEvaluatorSandbox.STRIPPED_BUILTINS == expected, (
        f"STRIPPED_BUILTINS mismatch. "
        f"Extra: {PythonEvaluatorSandbox.STRIPPED_BUILTINS - expected}, "
        f"Missing: {expected - PythonEvaluatorSandbox.STRIPPED_BUILTINS}"
    )
