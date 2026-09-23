//! Issue #1927 (AC2): every Rust code example embedded in
//! `analyze_graph.md`'s graph-mode documentation must compile through the
//! REAL evaluator pipeline (`xray_core::compiler::compile_evaluator`) --
//! the exact function production code uses to turn a submitted
//! `evaluator_code` string into a loadable `.so`. A doc example that only
//! "looks like Rust" is worse than none: issue #1927 reported the
//! reachability example under "Directional asymmetry" declaring
//! `let mut strong_hops` and then referencing an undefined `verified_hops`
//! a few lines later -- a `rustc` `E0425` a reader only discovers after
//! copying the example into a real request. This test makes that class of
//! defect fail `cargo test` instead of shipping silently.
//!
//! REWORK (issue #1927 review round 2): the first version of this test
//! silently completed a partial ```rust block with test-injected stub
//! functions before compiling, which let a doc block presented as a
//! ready-to-copy template (the Directional asymmetry reachability
//! example) hide that it was never actually a complete, copy-pasteable
//! evaluator. That is fixed here: EVERY ```rust block is now compiled
//! verbatim -- no test-side function injection, stub-completion, or
//! wrapping of any kind. A block that is genuinely not a complete
//! evaluator (a struct-shape illustration, or a demonstration of the
//! single optional `fn refine` alone) must say so in the doc itself via
//! the explicit ```rust,fragment label -- there is no other way to opt a
//! block out of compilation, and an unlabelled or ambiguously-labelled
//! Rust-like fence is a doc defect that panics this test rather than
//! being silently skipped.
//!
//! DECLARED SCOPE (exactly what this file does, nothing more):
//!   - Enumerates EVERY fenced code block in the doc (every whole markdown
//!     line beginning with `` ``` `` -- after trimming leading whitespace
//!     -- opens a block; the next such line closes it). Backticks that
//!     merely appear inline within prose or within a block's own body
//!     text are never mistaken for a fence, since only whole lines are
//!     inspected. An opening fence with no matching closing line is a
//!     malformed doc and PANICS, never silently ignored.
//!   - A block whose info string is exactly `"rust"` is a COMPLETE
//!     evaluator: its body lines are joined and compiled through the real
//!     pipeline verbatim -- no stub injection, no wrapping, no test-side
//!     code modification -- and it MUST succeed.
//!   - A block whose info string is exactly `"rust,fragment"` is an
//!     explicitly, visibly marked non-evaluator fragment (a type-shape
//!     illustration such as `UserFact`/`GraphResult`/`ReduceFinding`/
//!     `FileContext`, or a demonstration of the single optional `fn
//!     refine` in isolation) -- it is NOT compiled, but IS counted, and is
//!     asserted to genuinely lack a complete `collect_facts`+
//!     `analyze_graph` pair (catching the case where a real, complete
//!     evaluator was mislabelled a fragment specifically to dodge
//!     compilation).
//!   - A block whose info string is `"json"` is parsed as JSON (a block
//!     that fails to parse at all is a real doc defect and PANICS,
//!     naming the line it opened on); when it carries an `evaluator_code`
//!     string field, that string is compiled through the real pipeline
//!     verbatim (this is the literal payload a fresh MCP client sends for
//!     the doc's Quick Start example) and MUST succeed. A well-formed JSON
//!     block with no such field (a response-shape example, not a
//!     request) is counted as JSON but not as an evaluator.
//!   - Any OTHER info string that looks Rust-like (contains "rust",
//!     case-insensitively, or is exactly "rs") but is not one of the two
//!     recognized labels above -- e.g. a typo'd ```Rust or ```rust,ignore
//!     -- PANICS naming the offending label and line: there is no silent
//!     fallback classification for an ambiguous Rust-like fence. Any
//!     label that is neither JSON nor Rust-like (there are none in this
//!     doc today) is out of scope and ignored.
//!   - The exact number of complete evaluators, fragments, and
//!     JSON-embedded `evaluator_code` payloads found is asserted with
//!     `assert_eq!` against named constants below, not a floor -- so
//!     silently removing an example (as well as silently adding one) both
//!     fail this test, per the review's explicit requirement.

use std::path::{Path, PathBuf};
use xray_core::compiler::compile_evaluator;

/// Path to the doc under test, resolved relative to this crate's manifest
/// dir (`CARGO_MANIFEST_DIR` is always `rust/xray-core` for this crate's
/// own tests) so the test works regardless of the invoking working
/// directory.
fn doc_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(
        "../../src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md",
    )
}

const FENCE: &str = "```";

/// One fenced code block: its raw info string (e.g. `"rust"`,
/// `"rust,fragment"`, `"json"`), its body (the lines between the opening
/// and closing fence, joined with `\n` -- compiled or parsed exactly as
/// joined, with no further modification), and the 1-based line number its
/// opening fence appeared on (for actionable panic/assert messages).
struct FencedBlock {
    label: String,
    body: String,
    opening_line: usize,
}

/// Enumerates EVERY fenced code block in `markdown`, regardless of
/// language label. Fence delimiters are recognized only as WHOLE markdown
/// lines beginning with `` ``` `` (after trimming leading whitespace) --
/// inline backticks inside prose or a block's own body text are never
/// mistaken for a fence. Panics on an opening fence with no matching
/// closing fence line -- a malformed doc is a real defect, not a
/// silently-skipped example.
fn extract_all_fenced_blocks(markdown: &str) -> Vec<FencedBlock> {
    let lines: Vec<&str> = markdown.lines().collect();
    let mut blocks = Vec::new();
    let mut i = 0usize;
    while i < lines.len() {
        let trimmed = lines[i].trim_start();
        if let Some(info) = trimmed.strip_prefix(FENCE) {
            let label = info.trim().to_string();
            let (body, next_i) = read_fenced_body(&lines, i, &label);
            blocks.push(FencedBlock { label, body, opening_line: i + 1 });
            i = next_i;
            continue;
        }
        i += 1;
    }
    blocks
}

/// Reads the body of a fence opened at `lines[open_i]`, up to (excluding)
/// its closing fence line. Returns the joined body and the index just
/// past the closing fence. Panics if no closing fence is found.
fn read_fenced_body(lines: &[&str], open_i: usize, label: &str) -> (String, usize) {
    let mut body_lines = Vec::new();
    let mut j = open_i + 1;
    while j < lines.len() {
        if lines[j].trim_start().starts_with(FENCE) {
            return (body_lines.join("\n"), j + 1);
        }
        body_lines.push(lines[j]);
        j += 1;
    }
    panic!(
        "unterminated ```{label} fenced block in doc, opened at line {}",
        open_i + 1
    );
}

/// Running tally of what `classify_block` found, checked against the
/// EXPECTED_* constants once the whole doc has been scanned.
#[derive(Default)]
struct BlockCounts {
    full_evaluators: usize,
    fragments: usize,
    json_evaluator_code: usize,
}

/// A ```rust block: a COMPLETE evaluator, compiled verbatim (zero
/// test-side injection, stub-completion, or wrapping) through the real
/// pipeline. Must succeed.
fn assert_full_evaluator_compiles(block: &FencedBlock, cache_dir: &Path) {
    let result = compile_evaluator(&block.body, cache_dir);
    assert!(
        result.is_ok(),
        "```rust block opened at line {} in analyze_graph.md failed to compile verbatim (no \
         stub injection) through the real evaluator pipeline: {:?}\n--- source ---\n{}",
        block.opening_line,
        result.err(),
        block.body,
    );
}

/// A ```rust,fragment block: not compiled, but must genuinely lack a
/// complete `collect_facts` + `analyze_graph` pair -- otherwise it was
/// mislabelled to dodge verbatim compilation.
fn assert_genuinely_a_fragment(block: &FencedBlock) {
    let has_collect_facts = block.body.contains("fn collect_facts");
    let has_analyze_graph = block.body.contains("fn analyze_graph");
    assert!(
        !(has_collect_facts && has_analyze_graph),
        "```rust,fragment block opened at line {} in analyze_graph.md defines BOTH fn \
         collect_facts and fn analyze_graph -- it is a COMPLETE evaluator and must be \
         labelled ```rust (compiled verbatim), not ```rust,fragment",
        block.opening_line,
    );
}

/// A ```json block: parsed as JSON (panics on invalid JSON); when it
/// carries an `evaluator_code` string field, that string is compiled
/// verbatim and must succeed. Returns true iff `evaluator_code` was
/// present (i.e. this block counts toward EXPECTED_JSON_EVALUATOR_CODE_
/// BLOCKS).
fn process_json_block(block: &FencedBlock, cache_dir: &Path) -> bool {
    let value: serde_json::Value = serde_json::from_str(&block.body).unwrap_or_else(|e| {
        panic!(
            "```json block opened at line {} in analyze_graph.md is not valid JSON: {e}\n\
             --- block ---\n{}",
            block.opening_line, block.body,
        )
    });
    let Some(evaluator_code) = value.get("evaluator_code").and_then(|v| v.as_str()) else {
        return false;
    };
    let result = compile_evaluator(evaluator_code, cache_dir);
    assert!(
        result.is_ok(),
        "evaluator_code embedded in ```json block opened at line {} in analyze_graph.md \
         failed to compile verbatim through the real evaluator pipeline: {:?}\n\
         --- source ---\n{}",
        block.opening_line,
        result.err(),
        evaluator_code,
    );
    true
}

/// Panics when `label` looks Rust-like (contains "rust" case-insensitively,
/// or is exactly "rs") but is neither of the two recognized labels -- an
/// ambiguous Rust-like fence must never be silently ignored.
fn reject_if_ambiguous_rust_like_label(label: &str, opening_line: usize) {
    let lower = label.to_lowercase();
    if lower.contains("rust") || lower == "rs" {
        panic!(
            "```{label} block opened at line {opening_line} in analyze_graph.md has an \
             unrecognized Rust-like fence label -- use exactly ```rust (a complete evaluator, \
             compiled verbatim) or ```rust,fragment (an explicitly marked non-evaluator \
             fragment); there is no silent fallback classification for an ambiguous Rust-like \
             fence"
        );
    }
}

/// Classifies and processes one fenced block, updating `counts`.
fn classify_block(block: FencedBlock, cache_dir: &Path, counts: &mut BlockCounts) {
    match block.label.as_str() {
        "rust" => {
            counts.full_evaluators += 1;
            assert_full_evaluator_compiles(&block, cache_dir);
        }
        "rust,fragment" => {
            counts.fragments += 1;
            assert_genuinely_a_fragment(&block);
        }
        "json" => {
            if process_json_block(&block, cache_dir) {
                counts.json_evaluator_code += 1;
            }
        }
        other => reject_if_ambiguous_rust_like_label(other, block.opening_line),
    }
}

/// Exact number of ```rust blocks (complete evaluators, compiled verbatim)
/// the doc must contain. A doc-level ADD or REMOVE of an example is a
/// review-worthy change; this assertion fails on either direction.
const EXPECTED_FULL_RUST_EVALUATORS: usize = 3;

/// Exact number of ```rust,fragment blocks (explicitly non-evaluator
/// type-shape illustrations / isolated `fn refine` demo) the doc must
/// contain.
const EXPECTED_RUST_FRAGMENTS: usize = 4;

/// Exact number of ```json blocks carrying an `evaluator_code` string
/// field (compiled verbatim) the doc must contain.
const EXPECTED_JSON_EVALUATOR_CODE_BLOCKS: usize = 1;

fn assert_expected_counts(counts: &BlockCounts) {
    assert_eq!(
        counts.full_evaluators, EXPECTED_FULL_RUST_EVALUATORS,
        "expected exactly {EXPECTED_FULL_RUST_EVALUATORS} ```rust (complete, verbatim-compiled) \
         evaluator blocks in analyze_graph.md -- found {}; did an example get added, removed, \
         or mislabelled?",
        counts.full_evaluators,
    );
    assert_eq!(
        counts.fragments, EXPECTED_RUST_FRAGMENTS,
        "expected exactly {EXPECTED_RUST_FRAGMENTS} ```rust,fragment blocks in \
         analyze_graph.md -- found {}; did a fragment get added, removed, or mislabelled?",
        counts.fragments,
    );
    assert_eq!(
        counts.json_evaluator_code, EXPECTED_JSON_EVALUATOR_CODE_BLOCKS,
        "expected exactly {EXPECTED_JSON_EVALUATOR_CODE_BLOCKS} ```json block(s) carrying an \
         evaluator_code field in analyze_graph.md -- found {}; did the Quick Start example \
         change shape?",
        counts.json_evaluator_code,
    );
}

#[test]
fn every_rust_evaluator_example_in_analyze_graph_doc_compiles() {
    let path = doc_path();
    let markdown = std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("failed to read {}: {}", path.display(), e));

    let dir = tempfile::tempdir().expect("create temp dir");
    let mut counts = BlockCounts::default();

    for block in extract_all_fenced_blocks(&markdown) {
        classify_block(block, dir.path(), &mut counts);
    }

    assert_expected_counts(&counts);
}
