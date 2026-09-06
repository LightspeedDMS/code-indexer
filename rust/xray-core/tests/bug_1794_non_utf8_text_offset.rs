//! Regression test for Bug #1794 — OwnedNode.text() silently returns WRONG
//! text when the source file contains a raw invalid UTF-8 byte BEFORE the
//! node being sliced.
//!
//! `build_from_ts_node` (owned_node.rs) converts the file with
//! `String::from_utf8_lossy` when the raw bytes are not valid UTF-8. That
//! conversion replaces each invalid byte with U+FFFD — a 3-byte sequence.
//! tree-sitter's byte offsets are computed against the RAW (pre-lossy)
//! bytes, so once the lossy `Arc<str>` diverges in length from the raw
//! source at the point of the invalid byte, every offset past that point is
//! stale: `text()` slices the wrong bytes.
//!
//! This test places one raw 0xFF byte inside a comment, followed by two
//! ASCII bytes ("12") before the real `def` declaration starts. That gap is
//! deliberately chosen so the stale (raw) offset lands on a valid — but
//! WRONG — UTF-8 char boundary in the lossy string, rather than tripping
//! `owned_node.rs`'s `unwrap_or("")` degrade path. This pins the "silently
//! wrong text" half of the bug, which the pre-existing
//! `non_utf8_bytes_parse_without_panicking_and_text_stays_safe` test in
//! `s0a_regression.rs` deliberately does NOT pin (see story #1789 discovery
//! note on bug #1794).
//!
//! Uses the real production path (`scanner::parse_file`) — no OwnedNode
//! struct literals, per the s0a_regression.rs convention.
//!
//! Expected text (`"def greet():\n    return 1"`, no trailing newline) was
//! confirmed empirically against tree-sitter-python's real node span for
//! this exact grammar/source shape on valid UTF-8 input, before writing
//! this assertion.

use std::io::Write;
use std::path::PathBuf;
use tempfile::TempDir;
use xray_core::scanner;

fn write_file(dir: &TempDir, name: &str, bytes: &[u8]) -> PathBuf {
    let path = dir.path().join(name);
    std::fs::File::create(&path).unwrap().write_all(bytes).unwrap();
    path
}

#[test]
fn text_is_correct_for_a_node_that_follows_an_invalid_utf8_byte() {
    let dir = TempDir::new().unwrap();
    // Byte layout (raw offsets): '#'=0 ' '=1 0xFF=2 '1'=3 '2'=4 '\n'=5 'd'=6...
    // The invalid byte at raw offset 2 is BEFORE the `def` declaration, which
    // starts at raw offset 6 — exactly the shape the bug requires: an
    // offset that lands past the lossy-conversion's length divergence.
    let source: Vec<u8> = b"# \xFF12\ndef greet():\n    return 1\n".to_vec();
    let path = write_file(&dir, "invalid_before_decl.py", &source);

    let root =
        scanner::parse_file(&path).expect("a file with one invalid UTF-8 byte must still parse");

    let functions = root.descendants_of_kind("function_definition");
    assert_eq!(functions.len(), 1, "expected exactly one function_definition node");

    assert_eq!(
        functions[0].text(),
        "def greet():\n    return 1",
        "text() must return the declaration's own raw bytes, not a slice \
         shifted by the lossy UTF-8 conversion's byte-length divergence"
    );
}

/// Proves the fix holds for the WHOLE file, not just the single node
/// immediately adjacent to the invalid byte: a length-diverging lossy
/// conversion (the pre-fix bug) would desynchronize every offset from the
/// invalid byte onward, corrupting BOTH declarations below, not just the
/// first one.
#[test]
fn text_is_correct_for_every_declaration_after_an_invalid_utf8_byte() {
    let dir = TempDir::new().unwrap();
    let source: Vec<u8> =
        b"# \xFF12\ndef first():\n    return 1\n\n\ndef second():\n    return 2\n".to_vec();
    let path = write_file(&dir, "invalid_before_many_decls.py", &source);

    let root =
        scanner::parse_file(&path).expect("a file with one invalid UTF-8 byte must still parse");

    let functions = root.descendants_of_kind("function_definition");
    assert_eq!(functions.len(), 2, "expected exactly two function_definition nodes");
    assert_eq!(functions[0].text(), "def first():\n    return 1");
    assert_eq!(functions[1].text(), "def second():\n    return 2");
}
