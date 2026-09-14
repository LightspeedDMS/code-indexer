//! Regression test for Bug #1795 -- OwnedNode's recursive tree walks
//! (`has_descendant_of_kind`, `descendants_of_kind`/`collect_descendants_of_kind`,
//! `build_recursive`, and the derived `Drop` glue) SIGABRT the whole process
//! on deeply nested real source. A stack overflow in Rust aborts the process
//! -- it cannot be caught, not even by `catch_unwind` -- so one pathologically
//! nested file kills the entire `xray-cli` process instead of failing that
//! one file gracefully.
//!
//! This test builds a REAL, deeply nested Python source file (50,000 levels
//! of `parenthesized_expression` wrapping one `integer` leaf) and parses it
//! through the real production path (`scanner::parse_file`). That is the
//! ONLY way to exercise `build_recursive` -- the synthetic
//! `new_node_for_test`/`new_leaf_for_test` constructors (see
//! `owned_node.rs`'s `s0a_primitive_tests` module) build a tree iteratively
//! and never call `build_recursive` at all.
//!
//! Depth 50,000 is chosen to prove real headroom well past the empirically
//! measured cliff reported in issue #1795 (OK at 8,000 levels, SIGABRT
//! between 8,000 and 12,000) -- not just barely past the exact failure
//! point.
//!
//! BEFORE the fix: this test SIGABRTs the whole test binary process while
//! parsing (inside `build_recursive`). That is the "uncatchable process
//! abort" the bug describes -- there is nothing a `#[should_panic]` or
//! `Result`-returning test could catch, because the process itself is
//! killed by a signal, not unwound. This test file is its own compiled test
//! binary (Rust convention: one binary per file under `tests/`), so an abort
//! here does not corrupt any other test file's results.
//!
//! AFTER the fix: the file parses successfully, the resulting tree is
//! traversed with `has_descendant_of_kind`/`descendants_of_kind` (exercising
//! those two iterative rewrites on a REAL parsed tree, not just a synthetic
//! chain), and the tree is dropped cleanly at the end of the test (exercising
//! the iterative `Drop` impl) -- all without aborting.

use std::io::Write;
use tempfile::TempDir;
use xray_core::scanner;

/// Depth well past the empirically measured 8,000-12,000 SIGABRT cliff
/// (issue #1795), to prove real headroom rather than a bare pass-by-a-hair.
const DEEP_NESTING_DEPTH: usize = 50_000;

#[test]
fn deeply_nested_real_source_parses_traverses_and_drops_without_aborting() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("deep_nesting.py");
    let mut file = std::fs::File::create(&path).unwrap();

    // "x = " + 50,000 "(" + "0" + 50,000 ")" -- a syntactically valid Python
    // expression whose parse tree is a chain of 50,000 nested
    // `parenthesized_expression` nodes wrapping one `integer` leaf.
    write!(file, "x = ").unwrap();
    for _ in 0..DEEP_NESTING_DEPTH {
        write!(file, "(").unwrap();
    }
    write!(file, "0").unwrap();
    for _ in 0..DEEP_NESTING_DEPTH {
        write!(file, ")").unwrap();
    }
    writeln!(file).unwrap();
    file.flush().unwrap();
    drop(file);

    // Exercises build_recursive (site #3): pre-fix, this call SIGABRTs the
    // process while walking down 50,000 levels of tree-sitter children.
    let root = scanner::parse_file(&path)
        .expect("a deeply nested but syntactically valid Python file must parse");

    // Exercises has_descendant_of_kind (site #1): pre-fix, recurses to depth
    // ~50,000 and SIGABRTs.
    assert!(
        root.has_descendant_of_kind("integer"),
        "must find the innermost integer literal through 50,000 levels of nesting"
    );
    assert!(!root.has_descendant_of_kind("this_kind_does_not_exist_anywhere"));

    // Exercises descendants_of_kind / collect_descendants_of_kind (site #2).
    let integers = root.descendants_of_kind("integer");
    assert_eq!(integers.len(), 1, "exactly one integer leaf at the bottom of the chain");
    assert_eq!(integers[0].text(), "0");

    let parens = root.descendants_of_kind("parenthesized_expression");
    assert_eq!(
        parens.len(),
        DEEP_NESTING_DEPTH,
        "one parenthesized_expression node per nesting level"
    );

    // Exercises the derived Drop glue (site #4): `root` drops at the end of
    // this test, walking all 50,000 levels of `children: Vec<OwnedNode>`.
}
