//! Issue #1934: rustc diagnostic line-number rewriting, extracted verbatim
//! out of `compiler.rs` (pure move -- no behaviour change, see the module
//! doc comment on `super`).

/// Adjust rustc error line numbers by subtracting the preamble offset.
///
/// Rewrites TWO distinct KINDS of line-number occurrence rustc emits for
/// the same diagnostic (Bug #1827 -- a gutter row was previously left
/// unadjusted while the arrow row correctly shifted, diverging by exactly
/// `preamble_lines`, e.g. an arrow reading ":123:18" next to a gutter
/// reading "223 |"). NOTE: the arrow row and its OWN matching gutter row
/// (the row rustc prints for the exact line the arrow points at) must
/// agree after adjustment -- but a SINGLE diagnostic can carry SEVERAL
/// gutter rows referencing DIFFERENT source lines (a multi-line span, a
/// `note:` block, or a `help:` suggestion with several replacement rows),
/// each of which is adjusted independently and is not expected to equal
/// the arrow's own line number.
///  1. The "--> filename.rs:LINE:COL" arrow line -- recognized by its
///     OWN leading `-->` token (H-2: after trimming leading whitespace,
///     never by searching for "--> " anywhere in the line, which would
///     also match a gutter row whose ECHOED USER SOURCE happens to
///     contain that substring, e.g. a string literal referencing an
///     arrow -- misclassifying it as an arrow line would both corrupt
///     the user's own text via the digit-substitution below AND skip
///     `adjust_gutter_line` for that row entirely).
///  2. Numbered source-context "gutter" lines rustc prints alongside the
///     offending code, e.g. "123 |     let x: i32 = ...;" -- see
///     `adjust_gutter_line` below.
pub fn adjust_error_lines(stderr: &str, preamble_lines: usize) -> Vec<String> {
    let mut result = Vec::new();
    for line in stderr.lines() {
        // rustc arrow lines look like: "  --> filename.rs:LINE:COL" -- the
        // "-->" token is ALWAYS the first non-whitespace content on the
        // line. H-2: anchoring to the line's own leading token (after
        // trimming) instead of `line.find("--> ")` (which matched ANYWHERE
        // in the line) makes misclassifying a gutter row as an arrow row
        // structurally impossible -- a genuine gutter row always starts
        // with digits/whitespace/a bar character, never literally "-->".
        if let Some(after) = line.trim_start().strip_prefix("--> ") {
            if let Some(colon1) = after.find(':') {
                let after_colon1 = &after[colon1 + 1..];
                if let Some(colon2) = after_colon1.find(':') {
                    let line_str = &after_colon1[..colon2];
                    if let Ok(orig_line) = line_str.parse::<usize>() {
                        let adjusted = orig_line.saturating_sub(preamble_lines);
                        let new_line = line.replacen(
                            &format!(":{}", orig_line),
                            &format!(":{}", adjusted),
                            1,
                        );
                        result.push(new_line);
                        continue;
                    }
                }
            }
        }
        if let Some(adjusted) = adjust_gutter_line(line, preamble_lines) {
            result.push(adjusted);
            continue;
        }
        result.push(line.to_string());
    }
    result
}

/// Rewrites a rustc source-context "gutter" line -- a numbered line rustc
/// prints alongside a "-->" arrow line (or inside a `help:`/`note:` block)
/// to show real source content, e.g.:
///
/// ```text
/// error[E0308]: mismatched types
///   --> evaluator.rs:23:18
///    |
/// 23 |     let x: i32 = "not an integer";
///    |                  ^^^^^^^^^^^^^^^^ expected `i32`, found `&str`
/// help: try using a conversion method
///    |
/// 23 -     let x: i32 = "not an integer";
/// 23 +     let x: i32 = 5;
///    |
/// ```
///
/// Bug #1827: the arrow line's `23` above is adjusted by the loop in
/// `adjust_error_lines`, but each numbered gutter line's leading `23 |`/
/// `23 -`/`23 +` is a SEPARATE occurrence of a (potentially unadjusted,
/// PREAMBLE-shifted) line number -- left untouched, it disagrees with the
/// arrow by exactly `preamble_lines`.
///
/// H-1: rustc's OWN renderer uses FOUR different bar characters in this
/// gutter column, not just `|` -- `|` for a plain source-context row, `~`
/// for a `help:` block's REPLACED row, `+` for an INSERTED row, and `-`
/// for a REMOVED row (all verified live against the pinned toolchain; see
/// the real-compile tests in this module). Accepting only `|` left every
/// `~`/`+`/`-` row at its raw, PREAMBLE-shifted number while the REST of
/// the same diagnostic correctly adjusted -- an internally
/// self-contradictory diagnostic.
///
/// This recognizes a gutter line as `<leading whitespace><digits><optional
/// whitespace><bar><rest>` (bar in `['|', '~', '+', '-']`) and rewrites
/// ONLY the digit run, subtracting `preamble_lines` the same way the arrow
/// line's number is adjusted. Returns `None` for any line that is not a
/// genuine numbered gutter row (e.g. the plain `   |` caret/underline
/// continuation line, which has no leading digits, an unrelated line like
/// "10 warnings emitted" that has digits but no following bar character,
/// or a highlight/underline row like `   ++++++++++++` which has a bar
/// character but no leading digits at all) so the caller leaves it
/// untouched.
pub(crate) fn adjust_gutter_line(line: &str, preamble_lines: usize) -> Option<String> {
    let indent_len = line.len() - line.trim_start().len();
    let (leading_ws, rest) = line.split_at(indent_len);
    let digit_len = rest
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(rest.len());
    if digit_len == 0 {
        return None; // no leading digits -- not a gutter row
    }
    let digits = &rest[..digit_len];
    let after_digits = &rest[digit_len..];
    let gap_len = after_digits.len() - after_digits.trim_start().len();
    let (gap, remainder) = after_digits.split_at(gap_len);
    if !matches!(remainder.chars().next(), Some('|' | '~' | '+' | '-')) {
        return None; // digits not immediately followed by a gutter bar
    }
    let orig_line: usize = digits.parse().ok()?;
    let adjusted = orig_line.saturating_sub(preamble_lines).to_string();
    // Re-pad the adjusted number to the SAME digit-field width as the
    // original so the gutter bar column does not visually shift --
    // adjusted is always <= orig_line, so it never needs MORE digits.
    let padding = " ".repeat(digits.len().saturating_sub(adjusted.len()));
    Some(format!("{leading_ws}{padding}{adjusted}{gap}{remainder}"))
}
