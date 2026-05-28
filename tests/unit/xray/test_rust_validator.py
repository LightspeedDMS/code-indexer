"""Tests for Rust evaluator validator (Epic #1019 — pure Rust xray engine).

Covers:
- Valid Rust evaluator code passes validation
- All forbidden constructs rejected via parametrize:
  unsafe, std::fs/net/process/env/io, raw pointers (*const, *mut),
  extern blocks, mod declarations, static mut, missing fn evaluate_node,
  forbidden macros (include!, env!, println!, eprintln!, panic!, todo!, unimplemented!)
- ValidationResult structure has all required fields
"""

from __future__ import annotations

import pytest

from code_indexer.xray.sandbox import ValidationResult, validate_rust_evaluator

# ---------------------------------------------------------------------------
# Valid evaluator fixtures
# ---------------------------------------------------------------------------

VALID_EVALUATOR = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"""

VALID_EVALUATOR_WITH_LOGIC = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for child in node.named_children() {
        if child.kind == "method_invocation" {
            findings.push(EvalFinding {
                pattern: "method_call".to_string(),
                line: child.start_line,
                snippet: child.text.clone(),
            });
        }
    }
    findings
}
"""

# Shared unsafe evaluator used by structure-assertion tests.
_UNSAFE_EVALUATOR_INLINE = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    unsafe {}
    Vec::new()
}
"""

# ---------------------------------------------------------------------------
# Forbidden construct parametrize table
# Each entry: (case_id, expected_keyword_in_result, rust_source)
# Network fixture uses a non-routable dummy host string to avoid env coupling.
# ---------------------------------------------------------------------------

_FORBIDDEN_CASES: list[tuple[str, str, str]] = [
    (
        "unsafe_block",
        "unsafe",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    unsafe { let _ = 0usize as *const u8; }
    Vec::new()
}
""",
    ),
    (
        "std_fs",
        "std::fs",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = std::fs::read_to_string("dummy.txt");
    Vec::new()
}
""",
    ),
    (
        "std_net",
        "std::net",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = std::net::TcpStream::connect("dummy-host:0");
    Vec::new()
}
""",
    ),
    (
        "std_process",
        "std::process",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    std::process::exit(0);
    Vec::new()
}
""",
    ),
    (
        "std_env",
        "std::env",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = std::env::var("DUMMY_VAR");
    Vec::new()
}
""",
    ),
    (
        "std_io",
        "std::io",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    use std::io::Write;
    Vec::new()
}
""",
    ),
    (
        "raw_ptr_const",
        "*const",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _p: *const u8;
    Vec::new()
}
""",
    ),
    (
        "raw_ptr_mut",
        "*mut",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _p: *mut u8;
    Vec::new()
}
""",
    ),
    (
        "extern_block",
        "extern",
        """\
extern "C" {
    fn dummy_extern() -> i32;
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
""",
    ),
    (
        "mod_declaration",
        "mod",
        """\
mod hidden_module {
    pub fn noop() {}
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
""",
    ),
    (
        "static_mut",
        "static",
        """\
static mut COUNTER: u64 = 0;
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
""",
    ),
    (
        "missing_fn_evaluate_node",
        "evaluate_node",
        """\
let x = 42;
""",
    ),
    (
        "macro_println",
        "println",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    println!("hello");
    Vec::new()
}
""",
    ),
    (
        "macro_eprintln",
        "eprintln",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    eprintln!("oops");
    Vec::new()
}
""",
    ),
    (
        "macro_panic",
        "panic",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    panic!("bad");
    Vec::new()
}
""",
    ),
    (
        "macro_todo",
        "todo",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    todo!()
}
""",
    ),
    (
        "macro_unimplemented",
        "unimplemented",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    unimplemented!()
}
""",
    ),
    (
        "macro_include",
        "include",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _data = include!("dummy_file.rs");
    Vec::new()
}
""",
    ),
    (
        "macro_env",
        "env",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _v = env!("DUMMY_VAR");
    Vec::new()
}
""",
    ),
    (
        "macro_include_str",
        "include_str",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _s = include_str!("dummy.txt");
    Vec::new()
}
""",
    ),
    (
        "macro_include_bytes",
        "include_bytes",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _b = include_bytes!("dummy.bin");
    Vec::new()
}
""",
    ),
    (
        "macro_option_env",
        "option_env",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _v = option_env!("DUMMY_VAR");
    Vec::new()
}
""",
    ),
    (
        "macro_print",
        "print",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    print!("hello");
    Vec::new()
}
""",
    ),
    (
        "macro_eprint",
        "eprint",
        """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    eprint!("oops");
    Vec::new()
}
""",
    ),
]


# ---------------------------------------------------------------------------
# Tests: valid code
# ---------------------------------------------------------------------------


def test_valid_rust_evaluator_passes() -> None:
    """A well-formed Rust evaluator with fn evaluate_node returns ok=True."""
    result = validate_rust_evaluator(VALID_EVALUATOR)
    assert result.ok is True
    assert result.reason is None


def test_valid_rust_evaluator_with_logic_passes() -> None:
    """A Rust evaluator with safe Rust logic returns ok=True."""
    result = validate_rust_evaluator(VALID_EVALUATOR_WITH_LOGIC)
    assert result.ok is True


# ---------------------------------------------------------------------------
# Tests: forbidden constructs (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,expected_keyword,code",
    [pytest.param(cid, kw, src, id=cid) for cid, kw, src in _FORBIDDEN_CASES],
)
def test_forbidden_construct_rejected(
    case_id: str, expected_keyword: str, code: str
) -> None:
    """Every forbidden construct produces ok=False with keyword in reason or offending_construct."""
    result = validate_rust_evaluator(code)
    assert result.ok is False, f"Expected rejection for case '{case_id}'"
    combined = ((result.reason or "") + (result.offending_construct or "")).lower()
    assert expected_keyword.lower() in combined, (
        f"Expected '{expected_keyword}' in reason/offending_construct for '{case_id}'. "
        f"Got reason={result.reason!r}, offending_construct={result.offending_construct!r}"
    )


# ---------------------------------------------------------------------------
# Tests: ValidationResult structure
# ---------------------------------------------------------------------------


def test_validation_result_is_correct_type() -> None:
    """validate_rust_evaluator returns a ValidationResult instance."""
    result = validate_rust_evaluator(VALID_EVALUATOR)
    assert isinstance(result, ValidationResult)
    for field in (
        "ok",
        "reason",
        "error_code",
        "offending_construct",
        "offending_line",
    ):
        assert hasattr(result, field), f"ValidationResult missing field '{field}'"


def test_valid_result_fields_are_none() -> None:
    """Valid result has reason=None, error_code=None, offending_construct=None."""
    result = validate_rust_evaluator(VALID_EVALUATOR)
    assert result.ok is True
    assert result.reason is None
    assert result.error_code is None
    assert result.offending_construct is None


def test_invalid_result_has_non_empty_error_code() -> None:
    """Invalid result has a non-empty string error_code."""
    result = validate_rust_evaluator(_UNSAFE_EVALUATOR_INLINE)
    assert result.ok is False
    assert isinstance(result.error_code, str)
    assert len(result.error_code) > 0


def test_offending_line_is_int_or_none() -> None:
    """offending_line is either a positive int or None."""
    result = validate_rust_evaluator(_UNSAFE_EVALUATOR_INLINE)
    assert result.offending_line is None or (
        isinstance(result.offending_line, int) and result.offending_line > 0
    )
