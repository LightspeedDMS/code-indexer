# ^ coupled cases with rust/xray-core/src/validator/tests_source_limits.rs; same limits, same boundaries

from __future__ import annotations

from code_indexer.xray.sandbox import validate_rust_evaluator

# ^ why literals 131_072 and 64, not the module constants; these tests pin the values
_PINNED_MAX_SOURCE_BYTES = 131_072
_PINNED_MAX_BRACKET_DEPTH = 64

_ENTRY = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }\n"


def _source_of_exact_bytes(size: int, filler: str = "x") -> str:
    head = _ENTRY + "// "
    remaining = size - len(head.encode("utf-8"))
    width = len(filler.encode("utf-8"))
    source = head + filler * (remaining // width) + "x" * (remaining % width)
    assert len(source.encode("utf-8")) == size
    return source


def _nested_source(depth: int) -> str:
    opens, closes = "([{", ")]}"
    inner = depth - 1
    open_run = "".join(opens[i % 3] for i in range(inner))
    close_run = "".join(closes[i % 3] for i in reversed(range(inner)))
    return _ENTRY + "fn f() {\n    let _ = " + open_run + "1" + close_run + ";\n}\n"


def test_source_at_the_size_limit_is_judged_normally() -> None:
    result = validate_rust_evaluator(_source_of_exact_bytes(_PINNED_MAX_SOURCE_BYTES))
    assert result.ok is True, result.reason


def test_source_one_byte_over_the_size_limit_is_rejected() -> None:
    result = validate_rust_evaluator(
        _source_of_exact_bytes(_PINNED_MAX_SOURCE_BYTES + 1)
    )
    assert result.ok is False
    assert result.error_code == "source_too_large"
    assert result.offending_line == 1
    assert "131073 bytes" in (result.reason or "")
    assert "maximum of 131072 bytes" in (result.reason or "")


def test_source_size_is_counted_in_utf8_bytes() -> None:
    source = _source_of_exact_bytes(_PINNED_MAX_SOURCE_BYTES + 1, filler="é")
    assert len(source) <= _PINNED_MAX_SOURCE_BYTES
    result = validate_rust_evaluator(source)
    assert result.ok is False
    assert result.error_code == "source_too_large"


def test_size_limit_is_checked_before_the_entry_point() -> None:
    result = validate_rust_evaluator("x" * (_PINNED_MAX_SOURCE_BYTES + 1))
    assert result.error_code == "source_too_large"


def test_bracket_depth_at_the_limit_is_judged_normally() -> None:
    result = validate_rust_evaluator(_nested_source(_PINNED_MAX_BRACKET_DEPTH))
    assert result.ok is True, result.reason


def test_bracket_depth_one_over_the_limit_is_rejected() -> None:
    result = validate_rust_evaluator(_nested_source(_PINNED_MAX_BRACKET_DEPTH + 1))
    assert result.ok is False
    assert result.error_code == "nesting_too_deep"
    assert result.offending_line == 3
    assert "nesting depth exceeds the maximum of 64" in (result.reason or "")


def test_deeply_nested_use_groups_are_rejected_cleanly() -> None:
    depth = _PINNED_MAX_BRACKET_DEPTH + 1
    use = "use " + "a::{" * depth + "x" + "}" * depth + ";\n"
    result = validate_rust_evaluator(use + _ENTRY)
    assert result.ok is False
    assert result.error_code == "nesting_too_deep"


def test_closing_brackets_lower_the_depth() -> None:
    body = "let _ = (1);" * (_PINNED_MAX_BRACKET_DEPTH * 2)
    result = validate_rust_evaluator(_ENTRY + "fn f() { " + body + " }\n")
    assert result.ok is True, result.reason
