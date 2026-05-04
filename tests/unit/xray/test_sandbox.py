"""Baseline tests for PythonEvaluatorSandbox (Story #970).

Covers the 16 testing-requirements from the spec acceptance criteria.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List

import pytest

from code_indexer.xray.sandbox import (
    EvalResult,
    PythonEvaluatorSandbox,
    ValidationResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_ROOT = Path(__file__).parent / "fixtures"


def _make_engine_and_node(source: str, language: str = "python"):
    """Return (engine, root_node) for the given source snippet."""
    from code_indexer.xray.ast_engine import AstSearchEngine

    engine = AstSearchEngine()
    root = engine.parse(source, language)
    return engine, root


def _java_root(source: str):
    """Parse Java source and return root XRayNode."""
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine().parse(source, "java")


# ---------------------------------------------------------------------------
# Validation tests (no subprocess)
# ---------------------------------------------------------------------------


class TestValidation:
    def test_accepts_simple_node_type_check(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("return node.type == 'X'")
        assert isinstance(result, ValidationResult)
        assert result.ok is True

    def test_rejects_import_os(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("import os")
        assert result.ok is False
        assert "Import" in result.reason

    def test_rejects_function_definition(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("def f(): pass")
        assert result.ok is False
        assert "FunctionDef" in result.reason

    def test_rejects_lambda(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("lambda x: x")
        assert result.ok is False
        assert "Lambda" in result.reason

    def test_rejects_try_except(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("try:\n    1\nexcept:\n    2")
        assert result.ok is False
        # Python 3.9 uses Try; newer may use TryStar too
        assert "Try" in result.reason or "ExceptHandler" in result.reason

    def test_rejects_for_loop(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("for x in range(10): pass")
        assert result.ok is False
        assert "For" in result.reason

    def test_rejects_while_loop(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("while True: pass")
        assert result.ok is False
        assert "While" in result.reason

    def test_rejects_with_statement(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("with open('x') as f: pass")
        assert result.ok is False
        assert "With" in result.reason

    def test_validation_result_ok_is_true_for_complex_but_allowed_code(self):
        sb = PythonEvaluatorSandbox()
        code = (
            "return node.type == 'method_invocation' "
            "and len(source) > 0 "
            "and lang in ('java', 'python')"
        )
        result = sb.validate(code)
        assert result.ok is True

    def test_validation_result_reason_is_none_when_ok(self):
        sb = PythonEvaluatorSandbox()
        result = sb.validate("return True")
        assert result.ok is True
        assert result.reason is None


# ---------------------------------------------------------------------------
# Run tests (subprocess involved)
# ---------------------------------------------------------------------------


class TestRun:
    def _python_node_and_root(self, source: str = "x = 1"):
        _, root = _make_engine_and_node(source)
        # grab any named child as node, or root itself
        children = root.named_children
        node = children[0] if children else root
        return node, root

    def test_stripped_builtin_open_returns_subprocess_died(self):
        """open() is stripped; subprocess gets NameError => evaluator_subprocess_died."""
        sb = PythonEvaluatorSandbox()
        node, root = self._python_node_and_root()
        result = sb.run(
            "return open('/etc/passwd') is not None",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/test.py",
        )
        assert isinstance(result, EvalResult)
        assert result.failure == "evaluator_subprocess_died"

    def test_evaluator_returning_true(self):
        sb = PythonEvaluatorSandbox()
        _, root = _make_engine_and_node("def foo(): pass", "python")
        children = root.named_children
        node = children[0] if children else root
        result = sb.run(
            "return node.type == 'function_definition'",
            node=node,
            root=root,
            source="def foo(): pass",
            lang="python",
            file_path="/src/main.py",
        )
        assert result.failure is None
        assert result.value is True

    def test_evaluator_returning_false(self):
        sb = PythonEvaluatorSandbox()
        _, root = _make_engine_and_node("x = 1", "python")
        children = root.named_children
        node = children[0] if children else root
        result = sb.run(
            "return node.type == 'method_invocation'",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )
        assert result.failure is None
        assert result.value is False

    def test_evaluator_falsy_non_bool_coerced_to_false(self):
        """Return value 0 (falsy non-bool) should be coerced to False."""
        sb = PythonEvaluatorSandbox()
        node, root = self._python_node_and_root()
        result = sb.run(
            "return 0",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )
        # 0 is falsy — bool(0) == False; subprocess sends bool(result)
        assert result.failure is None
        assert result.value is False

    def test_api_exposes_node_root_source_lang_file_path(self):
        sb = PythonEvaluatorSandbox()
        source = "class Foo: pass"
        _, root = _make_engine_and_node(source, "python")
        node = root
        result = sb.run(
            "return lang == 'python' and len(source) > 0 and root.type == 'module' and '/src/' in file_path",
            node=node,
            root=root,
            source=source,
            lang="python",
            file_path="/src/main.py",
        )
        assert result.failure is None
        assert result.value is True

    def test_file_path_excludes_test_files(self):
        sb = PythonEvaluatorSandbox()
        source = "class FooTest: pass"
        _, root = _make_engine_and_node(source, "python")
        node = root
        result_test = sb.run(
            "return not file_path.endswith('Test.java')",
            node=node,
            root=root,
            source=source,
            lang="python",
            file_path="/src/FooTest.java",
        )
        assert result_test.failure is None
        assert result_test.value is False

        result_prod = sb.run(
            "return not file_path.endswith('Test.java')",
            node=node,
            root=root,
            source=source,
            lang="python",
            file_path="/src/Foo.java",
        )
        assert result_prod.failure is None
        assert result_prod.value is True

    def test_file_path_accessible_as_str(self):
        # isinstance is not in safe builtins; verify via str() roundtrip instead
        sb = PythonEvaluatorSandbox()
        node, root = self._python_node_and_root()
        result = sb.run(
            "return str(file_path) == file_path",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/test.py",
        )
        assert result.failure is None
        assert result.value is True

    def test_validation_failed_prevents_subprocess(self):
        """validate() failure must block subprocess — returns validation_failed."""
        sb = PythonEvaluatorSandbox()
        node, root = self._python_node_and_root()
        result = sb.run(
            "import os; return os.system('whoami') == 0",
            node=node,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
        )
        assert result.failure == "validation_failed"
        assert result.detail is not None
        assert "Import" in result.detail

    def test_os_exit_segfault_returns_subprocess_died(self):
        """Simulate segfault (exit code 139) => evaluator_subprocess_died."""
        node, root = self._python_node_and_root()
        # Use a code that calls os._exit via __builtins__ workaround —
        # but open/eval/exec are stripped. Instead, use an exception
        # path that causes non-zero exit. We use a code whose subprocess
        # naturally dies with a non-zero code through NameError on stripped
        # builtin, which the subprocess catches and sends as string payload.
        # To specifically test exitcode != 0, we rely on the subprocess dying
        # from a signal, which is tested in test_sandbox_lifecycle.py.
        # Here we test the general case: subprocess returns non-zero exitcode.
        # We cannot easily trigger os._exit inside whitelist-valid code.
        # So skip this as @pytest.mark.slow and do it in lifecycle tests.
        pytest.skip("Segfault simulation tested in test_sandbox_lifecycle.py")

    def test_concurrent_runs_do_not_interfere(self):
        """4 concurrent threads running distinct evaluators produce independent results."""
        sb = PythonEvaluatorSandbox()
        source = "x = 1"
        _, root = _make_engine_and_node(source, "python")
        node = root

        results: List[EvalResult] = [EvalResult(failure="not_run")] * 4
        errors: List[Exception] = []

        def worker(idx: int, code: str, expected: bool) -> None:
            try:
                r = sb.run(
                    code,
                    node=node,
                    root=root,
                    source=source,
                    lang="python",
                    file_path=f"/src/file_{idx}.py",
                )
                results[idx] = r
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(0, "return True", True)),
            threading.Thread(target=worker, args=(1, "return False", False)),
            threading.Thread(target=worker, args=(2, "return len(source) > 0", True)),
            threading.Thread(target=worker, args=(3, "return lang == 'python'", True)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Thread errors: {errors}"
        assert results[0].value is True
        assert results[1].value is False
        assert results[2].value is True
        assert results[3].value is True


# ---------------------------------------------------------------------------
# New match-position globals: match_byte_offset, match_line_number,
# match_line_content exposed to the evaluator subprocess
# ---------------------------------------------------------------------------


class TestMatchPositionGlobals:
    """sandbox.run exposes match_byte_offset/match_line_number/match_line_content."""

    def _root(self, source: str = "x = 1") -> "XRayNode":  # type: ignore[name-defined]
        _, root = _make_engine_and_node(source)
        return root

    def test_run_accepts_new_kwargs_with_none_defaults(self):
        """run() still works when new kwargs are omitted (backward compat)."""
        sb = PythonEvaluatorSandbox()
        root = self._root()
        result = sb.run(
            "return True",
            node=root,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
            # new kwargs intentionally omitted — must default to None, no error
        )
        assert result.failure is None
        assert result.value is True

    def test_match_byte_offset_int_is_visible_in_evaluator(self):
        """Evaluator can read match_byte_offset when an int value is passed."""
        sb = PythonEvaluatorSandbox()
        root = self._root()
        result = sb.run(
            "return match_byte_offset == 42",
            node=root,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
            match_byte_offset=42,
        )
        assert result.failure is None
        assert result.value is True

    def test_match_byte_offset_none_is_visible_as_none(self):
        """Evaluator sees match_byte_offset as None when None is passed."""
        sb = PythonEvaluatorSandbox()
        root = self._root()
        result = sb.run(
            "return match_byte_offset is None",
            node=root,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
            match_byte_offset=None,
        )
        assert result.failure is None
        assert result.value is True

    def test_all_three_match_globals_accessible_together(self):
        """match_byte_offset, match_line_number, match_line_content all visible."""
        sb = PythonEvaluatorSandbox()
        root = self._root()
        code = (
            "return ("
            "match_byte_offset == 10 "
            "and match_line_number == 3 "
            "and match_line_content == 'foo()'"
            ")"
        )
        result = sb.run(
            code,
            node=root,
            root=root,
            source="x = 1",
            lang="python",
            file_path="/src/main.py",
            match_byte_offset=10,
            match_line_number=3,
            match_line_content="foo()",
        )
        assert result.failure is None
        assert result.value is True
