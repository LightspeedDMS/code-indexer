"""Python's pre-flight must not be MORE PERMISSIVE than the Rust validator.

Deliberately a separate file from
``test_python_rust_validator_conformance_r2_2.py``: that module is skipped
wholesale without a built ``xray-cli``, and these assertions need only the
Python blocklist. Putting them there would have made the regression they pin
invisible on every machine and in CI -- which is how the divergence survived.
"""

class TestPythonMatchesRustOnNestedStdPaths:
    """Python's pre-flight must not be MORE PERMISSIVE than Rust's validator.

    The Rust validator was fixed to match a forbidden std module at ANY depth
    (`std::os::unix::fs::symlink` reaches the filesystem with no `fs` second
    segment) and to reject `use std as s;`, which rebinds the whole crate past
    every check. Python's `_RUST_FORBIDDEN_PATTERNS` is a raw-text blocklist
    with the identical depth hole: `\\bstd::fs\\b` does not match the nested
    form, and it has no `std::os` entry at all.

    Divergence in this direction is the one that matters. Python runs first, so
    a payload Python accepts and Rust rejects surfaces as a confusing failure
    at the compile step instead of a clear validation error -- and anyone who
    reasons about the gate from the Python side concludes it is allowed.
    """

    NESTED_REACHES = [
        'fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { let _ = std::os::unix::fs::symlink("/a", "/b"); Vec::new() }',
        'fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { let _ = std::os::unix::net::UnixStream::connect("/s"); Vec::new() }',
        'fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { let _ = std::thread::spawn(|| {}); Vec::new() }',
        'fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { let _ = std::path::Path::new("/etc").read_dir(); Vec::new() }',
        'fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { let _ = std::backtrace::Backtrace::capture(); Vec::new() }',
    ]

    def test_nested_forbidden_std_paths_are_rejected(self) -> None:
        from code_indexer.xray.sandbox import validate_rust_evaluator

        for src in self.NESTED_REACHES:
            result = validate_rust_evaluator(src)
            assert not result.ok, f"Python accepted what Rust rejects:\n{src}"

    def test_renaming_the_std_root_is_rejected(self) -> None:
        """`use std as s;` then `s::fs::read_to_string(...)` -- no `std::fs`
        substring appears anywhere, so a text blocklist cannot see it."""
        from code_indexer.xray.sandbox import validate_rust_evaluator

        src = (
            "use std as s;\n"
            "fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> "
            '{ let _ = s::fs::read_to_string("/etc/passwd"); Vec::new() }'
        )
        assert not validate_rust_evaluator(src).ok

    def test_legitimate_evaluators_are_untouched(self) -> None:
        """The shipped corpus uses std::collections and nothing else; a blocklist
        that rejects real evaluators is worse than useless."""
        from code_indexer.xray.sandbox import validate_rust_evaluator

        for src in (
            "fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }",
            "use std::collections::HashMap;\n"
            "fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> "
            "{ let m: HashMap<String, usize> = HashMap::new(); let _ = m.len(); Vec::new() }",
        ):
            assert validate_rust_evaluator(src).ok, src



class TestExternCrateParity:
    """`extern crate std as s;` must be rejected on BOTH sides.

    Python already rejects it via the broad `\\bextern\\b` pattern, so this is
    not a live leak -- but that was luck, not design: nothing pinned it, and
    the Rust validator (the authoritative gate, and the only one on a path that
    reaches xray-cli without the Python pre-check) accepted it outright. This
    test exists so the parity is checked rather than coincidental.
    """

    def test_extern_crate_is_rejected(self) -> None:
        from code_indexer.xray.sandbox import validate_rust_evaluator

        for src in (
            "extern crate std as s;\n"
            "fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> "
            '{ let _ = s::fs::read_to_string("/etc/passwd"); Vec::new() }',
            "extern crate alloc as a;\n"
            "fn evaluate_node(n: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }",
        ):
            assert not validate_rust_evaluator(src).ok, src
