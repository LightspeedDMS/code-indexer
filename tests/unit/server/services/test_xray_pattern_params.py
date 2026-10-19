"""Typed pattern parameters always produce well-formed Rust consts."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from code_indexer.server.services import xray_pattern_params, xray_pattern_service
from code_indexer.server.services.xray_pattern_params import (
    build_const_lines,
    resolve_params,
    validate_parameter_declarations,
)

U64_MAX = 2**64 - 1
I64_MIN = -(2**63)
I64_MAX = 2**63 - 1


def _decl(name: str, type_: str, default: Any) -> Dict[str, Any]:
    return {"name": name, "type": type_, "default": default}


def _const(type_: str, value: Any, name: str = "VALUE") -> str:
    decls: List[Dict[str, Any]] = [_decl(name, type_, value)]
    return build_const_lines(decls, {name: value})


def test_a_double_quote_is_escaped() -> None:
    assert _const("str", 'a"b') == 'const VALUE: &str = "a\\"b";'


def test_a_backslash_is_escaped() -> None:
    assert _const("str", "a\\b") == 'const VALUE: &str = "a\\\\b";'


def test_control_characters_are_escaped() -> None:
    assert _const("str", "a\nb\x7f") == 'const VALUE: &str = "a\\u{a}b\\u{7f}";'


def test_plain_text_is_unchanged() -> None:
    assert _const("str", "snippet") == 'const VALUE: &str = "snippet";'


@pytest.mark.parametrize("name", ["lower", "BAD-NAME", "1ABC", "", "A B"])
def test_a_new_store_refuses_non_const_names(name: str) -> None:
    error = validate_parameter_declarations([_decl(name, "usize", 1)], new_store=True)
    assert error is not None and error["error"] == "invalid_parameter"


def test_loading_accepts_a_lowercase_identifier() -> None:
    decls = [_decl("depth_threshold", "usize", 1)]
    assert validate_parameter_declarations(decls, new_store=False) is None
    assert _const("usize", 1, name="depth_threshold") == (
        "const depth_threshold: usize = 1;"
    )


@pytest.mark.parametrize("name", ["BAD-NAME", "X;fn", "1ABC", "A B"])
def test_punctuation_is_refused_everywhere(name: str) -> None:
    error = validate_parameter_declarations([_decl(name, "usize", 1)], new_store=False)
    assert error is not None and error["error"] == "invalid_parameter"
    with pytest.raises(ValueError):
        _const("usize", 1, name=name)


def test_a_stored_legacy_pattern_with_a_lowercase_name_still_loads(tmp_path) -> None:
    cidx_meta = tmp_path / "cidx-meta"
    target = cidx_meta / "xray-patterns" / "__any__" / "legacy-lower.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "name: legacy-lower\n"
        'description: "legacy"\n'
        "language: java\n"
        "parameters:\n"
        "  - name: depth\n"
        "    type: usize\n"
        "    default: 2\n"
        "evaluator_code: |\n"
        "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
    )
    code, _ = xray_pattern_service.XrayPatternService(
        cidx_meta
    ).resolve_and_prepare_pattern("example-repo-global", "legacy-lower")
    assert "const depth: usize = 2;" in code


@pytest.mark.parametrize(
    "type_,value",
    [
        ("usize", -1),
        ("usize", U64_MAX + 1),
        ("i64", I64_MIN - 1),
        ("i64", I64_MAX + 1),
        ("f64", float("nan")),
        ("f64", float("inf")),
        ("f64", float("-inf")),
    ],
)
def test_out_of_range_values_are_rejected(type_: str, value: Any) -> None:
    with pytest.raises(ValueError, match="parameter_type_mismatch"):
        resolve_params([_decl("VALUE", type_, 0)], {"VALUE": value})


@pytest.mark.parametrize(
    "type_,value",
    [
        ("usize", 0),
        ("usize", U64_MAX),
        ("i64", I64_MIN),
        ("i64", I64_MAX),
        ("f64", 1.5),
    ],
)
def test_bounds_are_inclusive(type_: str, value: Any) -> None:
    assert resolve_params([_decl("VALUE", type_, 0)], {"VALUE": value}) == {
        "VALUE": value
    }


def test_a_bad_default_is_rejected_at_declaration() -> None:
    error = validate_parameter_declarations(
        [_decl("DEPTH", "usize", -3)], new_store=True
    )
    assert error is not None and error["error"] == "invalid_parameter"


@pytest.mark.parametrize("new_store", [True, False])
def test_a_trailing_newline_is_refused_by_every_name_rule(new_store: bool) -> None:
    error = validate_parameter_declarations(
        [_decl("DEPTH\n", "usize", 3)], new_store=new_store
    )
    assert error is not None and error["error"] == "invalid_parameter"
    with pytest.raises(ValueError):
        _const("usize", 3, name="depth\n")


def test_an_f64_too_large_for_a_float_is_rejected() -> None:
    error = validate_parameter_declarations(
        [_decl("RATIO", "f64", 10**400)], new_store=True
    )
    assert error is not None and error["error"] == "invalid_parameter"
    with pytest.raises(ValueError, match="parameter_type_mismatch"):
        resolve_params([_decl("RATIO", "f64", 0.5)], {"RATIO": 10**400})


@pytest.mark.parametrize(
    "value, expected", [(4, "4.0"), (10**30, "1e+30"), (-3, "-3.0"), (2.5, "2.5")]
)
def test_an_integer_f64_value_is_emitted_as_a_float_literal(
    value: Any, expected: str
) -> None:
    assert _const("f64", value) == f"const VALUE: f64 = {expected};"


def test_only_one_build_const_lines_exists() -> None:
    assert hasattr(xray_pattern_params, "build_const_lines")
    assert not hasattr(xray_pattern_service.XrayPatternService, "_build_const_lines")
    assert (
        xray_pattern_service.build_const_lines is xray_pattern_params.build_const_lines
    )
