"""Validation edge cases for PythonEvaluatorSandbox.validate()."""

from __future__ import annotations

import time

from code_indexer.xray.sandbox import PythonEvaluatorSandbox


class TestValidationEdges:
    def test_empty_string_no_crash(self) -> None:
        v = PythonEvaluatorSandbox().validate("")
        assert v is not None

    def test_whitespace_only_no_crash(self) -> None:
        v = PythonEvaluatorSandbox().validate("   \n\t  \n")
        assert v is not None

    def test_comment_only_no_crash(self) -> None:
        v = PythonEvaluatorSandbox().validate("# just a comment\n# another\n")
        assert v is not None

    def test_bare_constant_validates(self) -> None:
        v = PythonEvaluatorSandbox().validate("42")
        assert v.ok

    def test_syntax_error_returns_validation_failed(self) -> None:
        v = PythonEvaluatorSandbox().validate("def (((")
        assert not v.ok
        assert "syntax_error" in (v.reason or "").lower()

    def test_null_byte_in_code_handled_gracefully(self) -> None:
        v = PythonEvaluatorSandbox().validate("a\x00b")
        # Either rejected or syntax_error; MUST NOT crash
        assert v is not None

    def test_5000_clause_or_chain_validates_quickly(self) -> None:
        code = "return " + " or ".join(["node.type == 'foo'"] * 5000)
        start = time.monotonic()
        v = PythonEvaluatorSandbox().validate(code)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"Validation took {elapsed:.2f}s"
        assert v.ok

    def test_deeply_nested_call_handles_gracefully(self) -> None:
        code = "return " + "len(" * 100 + "node.text" + ")" * 100
        v = PythonEvaluatorSandbox().validate(code)
        assert v is not None  # Either ok or graceful failure
