//! Issue #1934: `compiler.rs`'s unit tests covering rustc diagnostic
//! line-number rewriting (Bug #1827), relocated verbatim out of its
//! single `#[cfg(test)] mod tests { ... }` body (declared at `compiler.rs`
//! via `#[cfg(test)] #[path = "tests_diagnostics.rs"] mod tests_diagnostics;`,
//! mirroring the pattern `graph/bind/resolve.rs` already uses) so
//! `compiler.rs` itself stays well under the project's line limit.

use super::*;
use tempfile::TempDir;

#[test]
fn test_adjust_error_lines() {
    // Preamble is 10 lines, user code spans up to line 30; original
    // error at line 15 falls inside that span and should adjust to 5.
    let stderr = "error[E0425]: cannot find value\n  --> /tmp/abc.rs:15:5\n  |";
    let adjusted = adjust_error_lines(stderr, 10, 30);
    let joined = adjusted.join("\n");
    assert!(joined.contains(":5:"), "line 15 - 10 preamble = line 5: got {}", joined);
    assert!(!joined.contains(":15:"), "original line 15 should be replaced");
}

/// Bug #1929 rework item 2 (Codex P2): this test used to be named
/// `test_adjust_error_lines_no_overflow` and asserted `adjust_error_lines`
/// SATURATES a preamble-origin location to line 0 -- pinning the exact
/// bug this fix removes. A diagnostic at line 3, with the user-code span
/// starting only after line 100, is genuinely PREAMBLE-origin (generated
/// support code, never anything the user wrote) -- it must keep its REAL
/// original line number (never a fabricated 0) and must be CLEARLY
/// labelled so it can never be mistaken for user code.
#[test]
fn test_adjust_error_lines_leaves_a_preamble_origin_location_unchanged_and_labelled() {
    let stderr = "  --> /tmp/abc.rs:3:1";
    let adjusted = adjust_error_lines(stderr, 100, 200);
    let joined = adjusted.join("\n");
    assert!(
        joined.contains(":3:"),
        "a preamble-origin line (3, outside the 100..=200 user-code span) must keep its REAL \
         original line number, never be zeroed: got {}",
        joined
    );
    assert!(
        joined.contains("evaluator support code"),
        "a preamble-origin location must be clearly labelled, never silently presented as if it \
         were user code: got {}",
        joined
    );
}

/// Bug #1827 (defect 2): a synthetic rustc-shaped diagnostic block --
/// an arrow line AND a numbered gutter row referencing the SAME raw
/// (PREAMBLE-shifted) line 223 -- must both adjust to 123, never leave
/// the gutter row at the unadjusted 223 while the arrow moves to 123.
#[test]
fn test_adjust_error_lines_adjusts_gutter_line_to_match_arrow() {
    let stderr = "error[E0308]: mismatched types\n  \
        --> evaluator.rs:223:18\n    \
        |\n\
        223 |     let x: i32 = \"not an integer\";\n    \
        |                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ expected `i32`, found `&str`\n";
    let adjusted = adjust_error_lines(stderr, 100, 300);

    assert!(
        adjusted.iter().any(|l| l.contains(":123:18")),
        "arrow line must adjust to 123: got {:?}",
        adjusted
    );
    assert!(
        !adjusted.iter().any(|l| l.contains(":223:")),
        "raw arrow line 223 must not survive: got {:?}",
        adjusted
    );
    assert!(
        adjusted.iter().any(|l| l.trim_start().starts_with("123 |")),
        "gutter line must be adjusted to 123, matching the arrow: got {:?}",
        adjusted
    );
    assert!(
        !adjusted.iter().any(|l| l.trim_start().starts_with("223 |")),
        "raw gutter line 223 must not survive: got {:?}",
        adjusted
    );
}

/// Bug #1929 rework item 2 (Codex P2, the mirror-image case): a
/// diagnostic located AFTER the user-code span is EPILOGUE-origin --
/// generated support code the user never wrote. Before this fix, a
/// blind `saturating_sub` could still produce a SMALL POSITIVE number
/// here (e.g. an epilogue line only slightly past the user code's own
/// end), which LOOKS exactly like a plausible real user line but is
/// entirely fabricated -- strictly worse than the preamble case's
/// obvious `:0:`, since nothing about the output signals it is wrong.
/// The fix must leave it with its REAL original line number and the
/// same clear label.
#[test]
fn test_adjust_error_lines_leaves_an_epilogue_origin_location_unchanged_and_labelled() {
    // User code spans lines 11..=15 (preamble 10 lines); the diagnostic
    // at line 20 is past the end of user code -- epilogue-origin. A
    // pre-fix blind subtraction (20 - 10 = 10) would produce "10",
    // which LOOKS like a plausible line inside a 15-line user file but
    // is completely fabricated.
    let stderr = "  --> /tmp/abc.rs:20:1";
    let adjusted = adjust_error_lines(stderr, 10, 15);
    let joined = adjusted.join("\n");
    assert!(
        joined.contains(":20:"),
        "an epilogue-origin line (20, past the user-code span ending at 15) must keep its REAL \
         original line number, never be silently rewritten to the fabricated-but-plausible 10: \
         got {}",
        joined
    );
    assert!(
        !joined.contains(":10:"),
        "must never fabricate the plausible-but-wrong line 10: got {}",
        joined
    );
    assert!(
        joined.contains("evaluator support code"),
        "an epilogue-origin location must be clearly labelled, never silently presented as if it \
         were user code: got {}",
        joined
    );
}

/// Extracts the line number from the FIRST "--> file:LINE:COL" arrow
/// line found among `details` (helper for the discriminating test
/// below -- keeps the test itself focused on setup/assertions).
fn find_arrow_line_number(details: &[String]) -> usize {
    let arrow_line = details
        .iter()
        .find(|d| d.contains("--> "))
        .unwrap_or_else(|| panic!("expected a '--> ' arrow line in details: {:?}", details));
    arrow_line
        .rsplit("--> ")
        .next()
        .unwrap()
        .split(':')
        .nth(1)
        .unwrap_or_else(|| panic!("could not parse line number from arrow line: {}", arrow_line))
        .parse()
        .unwrap_or_else(|_| panic!("arrow line number not numeric: {}", arrow_line))
}

/// Extracts the line number from the FIRST numbered source-context
/// "gutter" row (e.g. "123 |     let x = ...;") found among `details`.
fn find_gutter_line_number(details: &[String]) -> usize {
    let gutter_line = details
        .iter()
        .find(|d| {
            let trimmed = d.trim_start();
            match trimmed.split_once('|') {
                Some((prefix, _)) => {
                    !prefix.trim().is_empty()
                        && prefix.trim().chars().all(|c| c.is_ascii_digit())
                }
                None => false,
            }
        })
        .unwrap_or_else(|| {
            panic!("expected a numbered gutter line in details: {:?}", details)
        });
    gutter_line
        .trim_start()
        .split_once('|')
        .unwrap()
        .0
        .trim()
        .parse()
        .unwrap_or_else(|_| panic!("gutter line number not numeric: {}", gutter_line))
}

/// Bug #1827 (defect 2, THE discriminating test): a REAL compile
/// failure's rustc diagnostic must report the SAME line number in its
/// "--> evaluator.rs:LINE:COL" arrow and its numbered source-context
/// gutter row -- both must point at the user's own source line, never
/// a PREAMBLE-shifted one. Compiles through the REAL compile_evaluator
/// pipeline (no mocking) with a deliberately non-compiling evaluator.
#[test]
fn test_compile_type_mismatch_arrow_and_gutter_line_numbers_agree() {
    let dir = TempDir::new().unwrap();
    let user_code = "\nfn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let x: i32 = \"this is deliberately not an integer\";\n    Vec::new()\n}\n";
    // user_code's own line count -- an UPPER bound any correctly
    // user-relative-adjusted line number must respect. The raw,
    // PREAMBLE-shifted line number (over 100, since PREAMBLE alone is
    // ~100 lines) would obviously violate this bound, so this is a
    // genuine, external proof that adjustment actually happened --
    // not a hardcoded guess at the exact assembled-source layout.
    let user_code_line_count = user_code.lines().count();

    let result = compile_evaluator(user_code, dir.path());
    assert!(result.is_err(), "a type mismatch must fail to compile");
    let err = result.unwrap_err();

    let arrow_line_number = find_arrow_line_number(&err.details);
    let gutter_line_number = find_gutter_line_number(&err.details);

    assert_eq!(
        arrow_line_number, gutter_line_number,
        "arrow line ({}) and gutter line ({}) must agree -- details: {:?}",
        arrow_line_number, gutter_line_number, err.details
    );
    assert!(
        arrow_line_number >= 1 && arrow_line_number <= user_code_line_count,
        "arrow/gutter line number ({}) must point at the user's OWN \
         source (1..={}), not a PREAMBLE-shifted absolute line: {:?}",
        arrow_line_number, user_code_line_count, err.details
    );
    // Codex H2 (Bug #1827 remediation): a genuine rustc compile
    // failure is the canonical Compile-kind error -- the agent's
    // evaluator source really is broken, and `details` carries the
    // real diagnostic it needs to fix it.
    assert_eq!(
        err.kind,
        CompileErrorKind::Compile,
        "a real rustc type-mismatch failure must classify as Compile"
    );
}

/// H-1 (Bug #1827 dual-review remediation): `adjust_gutter_line` must
/// recognize EVERY bar character rustc's own renderer uses in the
/// gutter column of a numbered source-context row -- not just `|`.
/// Real rustc `help:` suggestion blocks use `~` (replace a line),
/// `+` (insert a line) and `-` (remove a line) there, verified live
/// against the pinned toolchain (see the two real-compile tests
/// below). Before the fix, only `|` was accepted, so a `~`/`+`/`-`
/// row was left at its raw, PREAMBLE-shifted line number while every
/// `|` row in the SAME diagnostic correctly adjusted -- an
/// internally self-contradictory diagnostic, worse for the
/// agent-feedback loop than the uniform offset it replaced.
#[test]
fn test_adjust_gutter_line_accepts_tilde_plus_minus_and_pipe_bars() {
    for marker in ['|', '~', '+', '-'] {
        let line = format!("223 {}     replacement text", marker);
        let adjusted = adjust_gutter_line(&line, 100, 300).unwrap_or_else(|| {
            panic!("marker '{}' must be recognized as a gutter row", marker)
        });
        assert!(
            adjusted.trim_start().starts_with(&format!("123 {}", marker)),
            "marker '{}': expected line adjusted to 123, got {:?}",
            marker, adjusted
        );
    }
}

/// Finds every numbered gutter-style row in `details` whose bar
/// character is `marker` (e.g. '~' or '+'), returning each row's
/// (already-adjusted) line number. Generalizes `find_gutter_line_number`
/// (which is hardcoded to '|') to any marker, and collects ALL matches
/// instead of just the first -- a `help:` block commonly contains
/// several numbered replacement rows.
fn find_numbered_rows_with_marker(details: &[String], marker: char) -> Vec<usize> {
    details
        .iter()
        .filter_map(|d| {
            let trimmed = d.trim_start();
            let (prefix, _rest) = trimmed.split_once(marker)?;
            let prefix = prefix.trim();
            if prefix.is_empty() || !prefix.chars().all(|c| c.is_ascii_digit()) {
                return None;
            }
            prefix.parse().ok()
        })
        .collect()
}

/// H-1 (THE discriminating real-compile test for '~'): a genuine
/// non-exhaustive `match` (E0004) fails to compile through the REAL
/// `compile_evaluator` pipeline (no mocking), and rustc's own
/// "ensure that all possible cases are being handled" help: block
/// renders its suggested replacement arms with a `~` gutter marker --
/// verified live against the pinned toolchain before writing this
/// test. Both `~` rows must land inside the user's own source range
/// after adjustment, never left at their raw PREAMBLE-shifted value.
#[test]
fn test_compile_nonexhaustive_match_help_rows_use_tilde_and_are_adjusted() {
    let dir = TempDir::new().unwrap();
    let user_code = "\nfn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    enum Color { Red, Green, Blue }\n    let c = Color::Red;\n    let n = match c {\n        Color::Red => 1,\n    };\n    let _ = (node, n);\n    Vec::new()\n}\n";
    let user_code_line_count = user_code.lines().count();

    let result = compile_evaluator(user_code, dir.path());
    assert!(result.is_err(), "a non-exhaustive match must fail to compile");
    let err = result.unwrap_err();

    let tilde_rows = find_numbered_rows_with_marker(&err.details, '~');
    assert!(
        !tilde_rows.is_empty(),
        "expected at least one '~' gutter row in a real rustc help: \
         block: {:?}",
        err.details
    );
    for line_number in &tilde_rows {
        assert!(
            *line_number >= 1 && *line_number <= user_code_line_count,
            "tilde row line ({}) must be adjusted into the user's own \
             source range (1..={}), not left PREAMBLE-shifted: {:?}",
            line_number, user_code_line_count, err.details
        );
    }
}

/// Mechanical extraction from `test_compile_missing_trait_method_help_
/// rows_use_plus_and_minus_and_are_adjusted` (kept under the 50-line
/// function guideline): asserts the "similar name" suggestion's `-`/`+`
/// rows share the SAME real user source line (a full-line replace) --
/// a nonzero `+` row matching a `-` row's line number, not just that
/// some `+` and some `-` rows independently exist anywhere in the
/// diagnostic -- and that the paired line lands inside the user's own
/// source range.
fn assert_plus_minus_pair_within_range(
    plus_rows: &[usize],
    minus_rows: &[usize],
    user_code_line_count: usize,
    details: &[String],
) {
    let paired_line = minus_rows
        .iter()
        .find(|m| plus_rows.contains(m) && **m != 0)
        .copied();
    assert!(
        paired_line.is_some(),
        "expected a '-' row and a NONZERO '+' row sharing the same \
         adjusted line number (the 'similar name' full-line replace \
         pair): plus_rows={:?} minus_rows={:?} details={:?}",
        plus_rows, minus_rows, details
    );
    let paired_line = paired_line.unwrap();
    assert!(
        paired_line >= 1 && paired_line <= user_code_line_count,
        "'-'/'+' replacement pair line ({}) must be adjusted into the \
         user's own source range (1..={}), not left PREAMBLE-shifted: \
         {:?}",
        paired_line, user_code_line_count, details
    );
}

/// H-1 (real-compile test for '+' and '-'): calling a trait method
/// that is implemented but not imported (E0599, no `mod` needed --
/// avoids the sandbox validator's unrelated forbidden-construct ban)
/// fails to compile through the REAL compile_evaluator pipeline, and
/// rustc's own help: blocks render TWO distinct `+`/`-` shapes,
/// verified live against the pinned toolchain:
///  1. "perhaps you want to import it" suggests inserting `use std::fmt::
///     Write;` near the very top of the assembled PREAMBLE+user source
///     via a lone `+` row -- genuinely PREAMBLE-origin (Bug #1929 rework
///     item 2), so it must be left at its REAL raw line number, never
///     rewritten into the user's own range (nor the old, now-incorrect
///     "saturates to 0" behavior).
///  2. "there is a method `write_char` with a similar name" renders a
///     full-line REPLACE as a `-` row (old content) paired with a
///     NONZERO `+` row (new content) sharing the SAME real user
///     source line -- both must land inside the user's own range
///     after adjustment (see `assert_plus_minus_pair_within_range`).
#[test]
fn test_compile_missing_trait_method_help_rows_use_plus_and_minus_and_are_adjusted() {
    let dir = TempDir::new().unwrap();
    let user_code = "\nfn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let mut s = String::new();\n    let _ = s.write_str(\"hi\");\n    let _ = node;\n    Vec::new()\n}\n";
    let user_code_line_count = user_code.lines().count();

    let result = compile_evaluator(user_code, dir.path());
    assert!(
        result.is_err(),
        "calling an out-of-scope trait method must fail to compile"
    );
    let err = result.unwrap_err();

    // Bug #1929 rework item 2: the import-suggestion `+` row is genuinely
    // preamble-origin -- derive the REAL preamble boundary the same way
    // production code does (the structural bounds `assemble_evaluator_
    // source_with_preamble_and_bounds` returns) rather than hardcoding
    // the specific raw line number rustc happens to choose today.
    let (_assembled, (preamble_lines, _user_code_end)) =
        assemble_evaluator_source_with_preamble_and_bounds(PREAMBLE, user_code);
    let plus_rows = find_numbered_rows_with_marker(&err.details, '+');
    assert!(
        plus_rows.iter().any(|&n| n <= preamble_lines),
        "expected a '+' row for the suggested import whose RAW line number falls INSIDE the \
         preamble (<= {preamble_lines}) -- genuinely preamble-origin, so it must be left at its \
         real original line number, never silently rewritten into the user's own range: {:?}",
        err.details
    );

    let minus_rows = find_numbered_rows_with_marker(&err.details, '-');
    assert!(
        !minus_rows.is_empty(),
        "expected at least one '-' gutter row in a real rustc help: \
         block: {:?}",
        err.details
    );

    assert_plus_minus_pair_within_range(&plus_rows, &minus_rows, user_code_line_count, &err.details);
}

/// H-2 (Bug #1827 dual-review remediation, THE discriminating test):
/// the user's own evaluator source contains a "--> file:LINE:COL"
/// -shaped substring inside a string literal. Verified live against
/// the pinned toolchain: rustc echoes the user's source VERBATIM in
/// its numbered gutter row, so that row's text itself contains
/// "--> evaluator.rs:7:1". The OLD `line.find("--> ")` (matched
/// ANYWHERE in the line, not anchored to the line's own leading
/// token) misclassified this GUTTER row as an ARROW line: it
/// corrupted the embedded ":7" -> ":0" inside the user's own literal
/// via `replacen`, AND skipped `adjust_gutter_line` for the row
/// entirely -- leaving the real gutter line number PREAMBLE-shifted
/// (the Bug #1827 symptom, unfixed for this input). Both must be
/// false after the fix: the literal survives verbatim, and the row's
/// real line number is adjusted into the user's own source range.
#[test]
fn test_compile_evaluator_source_containing_arrow_token_is_not_corrupted_and_gutter_is_adjusted(
) {
    let dir = TempDir::new().unwrap();
    let user_code = "\nfn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {\n    let bad: i32 = \"path --> evaluator.rs:7:1 more text\";\n    let _ = (node, bad);\n    Vec::new()\n}\n";
    let user_code_line_count = user_code.lines().count();

    let result = compile_evaluator(user_code, dir.path());
    assert!(result.is_err(), "a type mismatch must fail to compile");
    let err = result.unwrap_err();

    let joined = err.details.join("\n");
    assert!(
        joined.contains("evaluator.rs:7:1"),
        "the user's own string literal must survive verbatim, \
         unmangled by the arrow-line rewrite: {:?}",
        err.details
    );

    let gutter_line_number = find_gutter_line_number(&err.details);
    assert!(
        gutter_line_number >= 1 && gutter_line_number <= user_code_line_count,
        "the gutter row for the user's actual source line must be \
         adjusted into range 1..={}, not left PREAMBLE-shifted \
         (H-2 -- the row must never be routed into the arrow branch \
         just because it CONTAINS '--> ' text): {:?}",
        user_code_line_count, err.details
    );
}

/// Bug #1929 rework item 2 (Codex P2), REAL GRAPH-MODE epilogue-origin
/// evidence: `collect_facts`'s own FIRST parameter type is deliberately
/// wrong (`i32` instead of the epilogue's own `&OwnedNode`) -- legal
/// syntax on its own (any parameter type compiles as a standalone
/// item), so the ONLY place the mismatch can surface is the call site
/// `GRAPH_EPILOGUE` itself emits (`collect_facts(node, file)` inside
/// `xray_collect_facts`) -- a genuine EPILOGUE-origin diagnostic, never
/// a user-code line. Proves the fix against the REAL graph-mode
/// assembled source, not a synthetic stderr string.
#[test]
fn test_compile_graph_mode_collect_facts_signature_mismatch_reports_an_epilogue_origin_diagnostic() {
    let dir = TempDir::new().unwrap();
    let user_code = "fn collect_facts(node: i32, file: &str) -> Vec<UserFact> {\n    let _ = (node, file);\n    Vec::new()\n}\nfn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {\n    let _ = (g, facts);\n    GraphResult::default()\n}\n";
    assert_eq!(
        detect_evaluator_mode(user_code).unwrap(),
        EvaluatorMode::Graph,
        "test fixture assumption broken: must still classify as Graph mode despite the \
         signature mismatch (mode detection only checks function NAMES, never signatures)"
    );

    let result = compile_evaluator(user_code, dir.path());
    let err = result.expect_err("a collect_facts signature mismatch must fail to compile");

    let (_assembled, (_preamble_lines, user_code_end)) =
        assemble_graph_evaluator_source_and_bounds(user_code);

    let arrow_line_number = find_arrow_line_number(&err.details);
    assert!(
        arrow_line_number > user_code_end,
        "the type mismatch is INSIDE the epilogue's own call to collect_facts, past the real \
         user-code end ({user_code_end}) -- must be reported at its REAL (unremapped) line \
         number, never rewritten into the user's own 1..={user_code_end} range: got \
         {arrow_line_number}, details: {:?}",
        err.details
    );
    assert!(
        err.details.iter().any(|d| d.contains("evaluator support code")),
        "an epilogue-origin diagnostic must be clearly labelled, never silently presented as if \
         it were a user-code line: {:?}",
        err.details
    );
}

/// Bug #1929 rework (Codex P2): user code containing an UNINDENTED
/// comment line that happens to be LITERALLY `// ---- END USER CODE
/// ----` (the exact marker text `assemble_with_epilogue` inserts --
/// plausible if a user copy-pastes an example from this very tool's
/// own documentation) must NEVER corrupt line-bound classification.
/// Bounds must be computed STRUCTURALLY, from the KNOWN line counts of
/// the pieces the assembler concatenates, never by re-parsing marker
/// text out of the finished assembled source -- a marker-search
/// implementation misclassifies the REAL error (well inside the
/// user's own code, after the injected lookalike line) as "evaluator
/// support code".
#[test]
fn test_compile_user_code_containing_literal_marker_text_does_not_corrupt_line_bounds() {
    let dir = TempDir::new().unwrap();
    let user_code = "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n// ---- END USER CODE ----\nVec::new()\n}\nfn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {\nlet x: i32 = \"deliberately not an integer\";\nlet _ = (g, facts, x);\nGraphResult::default()\n}\n";
    let user_code_line_count = user_code.lines().count();

    let result = compile_evaluator(user_code, dir.path());
    assert!(result.is_err(), "a type mismatch must fail to compile");
    let err = result.unwrap_err();

    let arrow_line_number = find_arrow_line_number(&err.details);
    assert!(
        arrow_line_number >= 1 && arrow_line_number <= user_code_line_count,
        "the real error must map into the user's own source range (1..={}), never truncated \
         or misclassified by the injected marker-lookalike comment: {:?}",
        user_code_line_count, err.details
    );
    assert!(
        !err.details.iter().any(|d| d.contains("evaluator support code")),
        "a genuine user-code error must never be labelled as evaluator support code just \
         because an earlier user line happens to match the marker text: {:?}",
        err.details
    );
}
