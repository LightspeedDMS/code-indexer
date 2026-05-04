"""Tests for AST nodes that ARE rejected by the whitelist but lack explicit guard tests."""

from __future__ import annotations

import sys

import pytest

from code_indexer.xray.sandbox import PythonEvaluatorSandbox


def _validate(code: str):
    return PythonEvaluatorSandbox().validate(code)


class TestUntestedASTRejections:
    """Each test asserts a whitelist-rejected AST node yields validation_failed."""

    def test_keyword_arg_in_call_rejected(self) -> None:
        v = _validate("return dict(a=1)")
        assert not v.ok
        assert "keyword" in (v.reason or "")

    def test_double_splat_kwargs_rejected(self) -> None:
        v = _validate("return dict(**{'a': 1})")
        assert not v.ok

    def test_slice_in_subscript_rejected(self) -> None:
        v = _validate("return node.text[1:2]")
        assert not v.ok
        assert "Slice" in (v.reason or "")

    def test_starred_in_call_rejected(self) -> None:
        v = _validate("return min(*[1, 2, 3])")
        assert not v.ok

    @pytest.mark.skipif(
        sys.version_info < (3, 10), reason="match requires Python 3.10+"
    )
    def test_match_statement_rejected(self) -> None:
        code = "match node:\n    case x:\n        return True\n"
        v = _validate(code)
        assert not v.ok

    @pytest.mark.skipif(sys.version_info < (3, 11), reason="except* requires 3.11+")
    def test_try_star_rejected(self) -> None:
        code = "try:\n    return True\nexcept* ValueError:\n    return False\n"
        v = _validate(code)
        assert not v.ok

    def test_typed_assignment_rejected(self) -> None:
        v = _validate("x: int = 1")
        assert not v.ok
