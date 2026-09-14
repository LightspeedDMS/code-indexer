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

from code_indexer.xray.sandbox import (
    ValidationResult,
    _RUST_FORBIDDEN_PATTERNS,
    validate_rust_evaluator,
)

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


def test_graph_mode_evaluator_with_both_collect_facts_and_analyze_graph_passes() -> (
    None
):
    """Story #1811 (AC2): ADR-001 fixes execution modes at exactly two --
    legacy (fn evaluate_node) and graph (fn collect_facts + fn analyze_graph).
    A well-formed graph-mode evaluator with NO fn evaluate_node at all must
    pass validation, not be rejected as missing_entry_point -- the exact gap
    that made graph mode unreachable from Python before this story.
    """
    code = (
        "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> "
        "{ Vec::new() }\n"
        "fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult "
        "{ GraphResult::default() }\n"
    )
    result = validate_rust_evaluator(code)
    assert result.ok is True, (
        f"a well-formed graph-mode evaluator must pass: {result.reason}"
    )
    assert result.reason is None


def test_graph_mode_evaluator_missing_analyze_graph_is_still_rejected() -> None:
    """Only fn collect_facts, with neither fn evaluate_node nor fn
    analyze_graph, satisfies NEITHER mode -- must still be rejected as
    missing_entry_point (Rust's own detect_evaluator_mode requires BOTH
    collect_facts and analyze_graph together for graph mode; this Python
    gate must not be more permissive than that).
    """
    code = "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }\n"
    result = validate_rust_evaluator(code)
    assert result.ok is False
    assert result.error_code == "missing_entry_point"


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


# ---------------------------------------------------------------------------
# Tests: plain static declaration (Issue 1)
# ---------------------------------------------------------------------------


def test_static_declaration_rejected() -> None:
    """Plain 'static' declaration (non-mut) must be rejected by pre-flight validator."""
    code = """\
static FOO: u64 = 0;
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is False, "Plain static declaration should be rejected"
    assert isinstance(result.error_code, str) and len(result.error_code) > 0
    assert "static" in result.error_code.lower(), (
        f"Expected error_code to contain 'static', got {result.error_code!r}"
    )
    combined = ((result.reason or "") + (result.offending_construct or "")).lower()
    assert "static" in combined, (
        f"Expected 'static' in reason/offending_construct. "
        f"Got reason={result.reason!r}, offending_construct={result.offending_construct!r}"
    )


def test_static_mut_still_rejected() -> None:
    """Regression: 'static mut' must still be rejected after adding plain static check."""
    code = """\
static mut COUNTER: u64 = 0;
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is False, "static mut should still be rejected"
    assert isinstance(result.error_code, str) and len(result.error_code) > 0
    assert "static" in result.error_code.lower(), (
        f"Expected error_code to contain 'static', got {result.error_code!r}"
    )


def test_static_in_string_not_rejected() -> None:
    """The word 'static' inside a string literal must not trigger a false positive."""
    code = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let label = "static analysis result";
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is True, (
        f"'static' inside a string literal should NOT be rejected. "
        f"Got ok={result.ok}, reason={result.reason!r}"
    )


# S0a: the front-door table is authoritative.  Keys deliberately use the
# stable (error_code, construct, regex) identity rather than test names or a
# count, so adding/reordering entries cannot silently lose coverage.
_EXPLICIT_FRONT_DOOR_FIXTURES = {
    expected_keyword: source for _, expected_keyword, source in _FORBIDDEN_CASES
}
for _macro in (
    "include",
    "env",
    "println",
    "eprintln",
    "panic",
    "todo",
    "unimplemented",
    "include_str",
    "include_bytes",
    "option_env",
    "print",
    "eprint",
):
    _EXPLICIT_FRONT_DOOR_FIXTURES[f"{_macro}!"] = _EXPLICIT_FRONT_DOOR_FIXTURES[_macro]
_EXPLICIT_FRONT_DOOR_FIXTURES["static"] = """\
static VALUE: u8 = 1;
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }
"""
# R2-2 (Codex re-review): macro_rules! must be rejected outright, even
# with a completely innocuous body (no other blocklisted keyword) -- this
# is the SAME fixture shape as test_macro_rules_definition_rejected above,
# kept deliberately isolated so the front-door meta-test discriminates on
# the macro_rules! ban itself.
_EXPLICIT_FRONT_DOOR_FIXTURES["macro_rules!"] = """\
macro_rules! innocuous_helper {
    () => {
        42
    };
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }
"""
_AUTHORITATIVE_FRONT_DOOR_CASES = {
    (error_code, construct, pattern): _EXPLICIT_FRONT_DOOR_FIXTURES[construct]
    for error_code, construct, pattern in _RUST_FORBIDDEN_PATTERNS
    if construct in _EXPLICIT_FRONT_DOOR_FIXTURES
}


@pytest.mark.parametrize("error_code,construct,pattern", _RUST_FORBIDDEN_PATTERNS)
def test_every_authoritative_front_door_entry_has_exact_outcome(
    error_code: str, construct: str, pattern: str
) -> None:
    result = validate_rust_evaluator(
        _AUTHORITATIVE_FRONT_DOOR_CASES[(error_code, construct, pattern)]
    )
    assert result.ok is False
    assert result.error_code == error_code
    assert result.offending_construct == construct


def test_front_door_meta_test_covers_authoritative_table() -> None:
    covered = set(_AUTHORITATIVE_FRONT_DOOR_CASES)
    assert covered == set(_RUST_FORBIDDEN_PATTERNS)


@pytest.mark.parametrize(
    "code,expected_ok",
    [
        (
            'fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let _ = "սafe"; Vec::new() }',
            True,
        ),
        (
            "// u\u0073afe\nfn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }",
            True,
        ),
        (
            # R3-1 (Codex re-review, ROUND 3): flipped from False to True.
            # A raw string literal's CONTENT is inert runtime data in real
            # Rust -- it is never parsed as code, regardless of what text
            # it contains -- so rejecting this was itself a false
            # positive of the exact class R3-1 fixes (a documented
            # scanner limitation, not a genuine security invariant: there
            # is no way to smuggle executable code inside a string
            # literal's contents).
            'fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let _ = r#"unsafe std::fs"#; Vec::new() }',
            True,
        ),
        (
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let r#unsafe = 1; let _ = r#unsafe; Vec::new() }",
            False,
        ),
        (
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { let _ = std /* comment */ ::fs::dummy; Vec::new() }",
            True,
        ),
    ],
)
def test_bypass_attempts_have_defined_front_door_outcomes(
    code: str, expected_ok: bool
) -> None:
    result = validate_rust_evaluator(code)
    assert result.ok is expected_ok


# ---------------------------------------------------------------------------
# R2-2 (Codex re-review): Python's macro check was a hole-y BLOCKLIST of
# specific names (println!, panic!, etc.) while Rust's validator.rs is a
# fail-closed ALLOWLIST (only vec!/format!/matches!) -- an arbitrary
# UNLISTED macro name, a QUALIFIED macro path, or a macro_rules!
# DEFINITION all sailed through Python's pre-flight only to be rejected
# late (and confusingly) by Rust's authoritative compile step.
# ---------------------------------------------------------------------------


def test_unlisted_macro_invocation_rejected_fail_closed() -> None:
    """assert! is not in Python's old named blocklist, but Rust's
    allowlist (vec!/format!/matches! only) rejects it -- Python must too."""
    code = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    assert!(node.start_line > 0);
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is False, (
        "assert! is not on the allowlist (vec!/format!/matches!) and must be "
        "rejected fail-closed, matching Rust's validator.rs -- Python was "
        "previously MORE PERMISSIVE than Rust here (a real cross-layer bug)"
    )


def test_qualified_macro_path_rejected() -> None:
    """A qualified macro path (evil::vec!) must be rejected even though the
    last segment matches an allowlisted name -- mirrors validator.rs's
    exact-path-match fix."""
    code = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _v: Vec<i32> = evil::vec![1, 2, 3];
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is False, (
        "a QUALIFIED macro invocation (evil::vec!) must be rejected -- only "
        "a bare, unqualified vec!/format!/matches! is permitted"
    )


def test_macro_rules_definition_rejected() -> None:
    """A macro_rules! definition must be rejected OUTRIGHT, regardless of
    how innocuous its body looks -- mirrors Rust's blanket ban philosophy
    ("never selectively whitelist macro_rules! bodies").

    Deliberately uses a body with NO other blocklisted keyword (no
    std::process/unsafe/etc.) so this test discriminates on the
    macro_rules! ban itself, not an unrelated keyword match that would
    happen to also appear inside the body."""
    code = """\
macro_rules! innocuous_helper {
    () => {
        42
    };
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = innocuous_helper!();
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is False, (
        "a macro_rules! definition must be rejected outright -- its body can "
        "smuggle any forbidden construct, and Rust's validator.rs rejects it "
        "unconditionally regardless of contents"
    )


# ---------------------------------------------------------------------------
# R3-1 (Codex re-review, ROUND 3): the textual macro-invocation regex scans
# RAW source, including string literals, comments, and Rust's boolean
# negation operator `!` following a bare keyword -- all three produce false
# POSITIVES (Python rejects valid Rust the authoritative Rust validator
# accepts). This is a real bug: Python rejecting valid Rust breaks users.
# ---------------------------------------------------------------------------


def test_negation_form_if_bang_paren_is_accepted() -> None:
    """`if !(expr) { }` is extremely common Rust (boolean negation) and
    must NOT be misread as a macro invocation named `if`."""
    code = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    if !(node.kind.is_empty()) {
        return Vec::new();
    }
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is True, (
        f"'if !(...)' is valid Rust boolean negation and must be accepted -- "
        f"got ok={result.ok}, reason={result.reason!r}, "
        f"offending_construct={result.offending_construct!r}"
    )


def test_macro_name_inside_string_literal_is_not_rejected() -> None:
    """A macro-name-shaped substring living inside a STRING LITERAL is
    data, not code, and must not trigger rejection."""
    code = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let s = "println!(\\"x\\")";
    let _ = s.len();
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is True, (
        f"a macro name inside a string literal must not be rejected -- "
        f"got ok={result.ok}, reason={result.reason!r}, "
        f"offending_construct={result.offending_construct!r}"
    )


def test_macro_name_inside_comment_is_not_rejected() -> None:
    """A macro-name-shaped substring living inside a COMMENT is not code
    and must not trigger rejection."""
    code = """\
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    // assert!(false)
    Vec::new()
}
"""
    result = validate_rust_evaluator(code)
    assert result.ok is True, (
        f"a macro name inside a comment must not be rejected -- "
        f"got ok={result.ok}, reason={result.reason!r}, "
        f"offending_construct={result.offending_construct!r}"
    )
