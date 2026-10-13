/// A finding produced by a scan, attached to a specific file path.
#[derive(Debug, Clone)]
pub struct Finding {
    pub pattern: String,
    pub file: String,
    pub line: usize,
    pub snippet: String,
    /// The full text of source line `line`, exactly as cidx's Python layer
    /// derives it (see `line_content_at`). `None` where no source is in hand
    /// (the graph `--refine` phase); the scan path always fills it, so cidx
    /// never has to open the file a second time to show the matched line.
    pub line_content: Option<String>,
}

/// A finding produced by an evaluator before it is associated with a file.
#[derive(Debug, Clone)]
pub struct EvalFinding {
    pub pattern: String,
    pub line: usize,
    pub snippet: String,
}

/// Collapse runs of whitespace in `s`, then truncate to `max_len` bytes.
/// Truncation always occurs on a UTF-8 char boundary so the result is valid UTF-8.
/// If truncation occurs, append "..." to the result.
pub fn truncate_snippet(s: &str, max_len: usize) -> String {
    let collapsed: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.len() <= max_len {
        collapsed
    } else {
        // Find the largest byte index that is <= max_len AND is a char boundary.
        let boundary = collapsed
            .char_indices()
            .map(|(i, _)| i)
            .take_while(|&i| i <= max_len)
            .last()
            .unwrap_or(0);
        format!("{}...", &collapsed[..boundary])
    }
}

/// `source` split into lines the way `line_content_at` indexes them, on
/// Python's `str.splitlines()` boundaries: `\n`, `\r`, `\r\n` (one
/// boundary), `\x0b`, `\x0c`, `\x1c`, `\x1d`, `\x1e`, `\x85`, `\u2028`,
/// `\u2029`. A trailing boundary does not open an extra empty line.
pub fn source_lines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let end_of_boundary = match c {
            '\r' => match chars.peek() {
                Some(&(j, '\n')) => {
                    chars.next();
                    j + 1
                }
                _ => i + 1,
            },
            '\n' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}'
            | '\u{2028}' | '\u{2029}' => i + c.len_utf8(),
            _ => continue,
        };
        lines.push(&s[start..i]);
        start = end_of_boundary;
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// Line `line` (1-based) of `lines` (from `source_lines`), or `""` when out of range.
///
/// Byte-for-byte what cidx's Python layer computed before the scan emitted
/// it: `source.decode("utf-8", errors="replace").splitlines()[line - 1]`.
/// `from_utf8_lossy` substitutes U+FFFD per maximal invalid subpart, as
/// Python's `replace` handler does.
pub fn line_content_at(lines: &[&str], line: usize) -> String {
    match line.checked_sub(1).and_then(|idx| lines.get(idx)) {
        Some(text) => (*text).to_string(),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Expected values below are Python's own output for the same bytes:
    //   src.decode("utf-8", errors="replace").splitlines()
    #[test]
    fn test_source_lines_match_python_splitlines_boundaries() {
        let src = b"a\r\nb\rc\nd\x0be\x0cf\x1cg\x1dh\x1ei\xc2\x85j\xe2\x80\xa8k\xe2\x80\xa9l\n\nm\n";
        let decoded = String::from_utf8_lossy(src);
        assert_eq!(
            source_lines(&decoded),
            vec!["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "", "m"]
        );
    }

    #[test]
    fn test_source_lines_replace_invalid_utf8_like_python() {
        let src = b"ok\xff\xfe\xed\xa0\x80z\xe2\x82\n\xf0\x9f\x8c\x8d\xf0\x9f\n\xc0\xafq\r\r\n";
        let decoded = String::from_utf8_lossy(src);
        assert_eq!(
            source_lines(&decoded),
            vec![
                "ok\u{fffd}\u{fffd}\u{fffd}\u{fffd}\u{fffd}z\u{fffd}",
                "\u{1f30d}\u{fffd}",
                "\u{fffd}\u{fffd}q",
                "",
            ]
        );
    }

    #[test]
    fn test_source_lines_edges_match_python() {
        assert!(source_lines("").is_empty());
        assert_eq!(source_lines("x"), vec!["x"]);
        assert_eq!(source_lines("\n"), vec![""]);
        assert_eq!(source_lines("\r\n\r\n"), vec!["", ""]);
    }

    #[test]
    fn test_line_content_at_is_one_based_and_empty_out_of_range() {
        let lines = source_lines("first\nsecond\n");
        assert_eq!(line_content_at(&lines, 1), "first");
        assert_eq!(line_content_at(&lines, 2), "second");
        assert_eq!(line_content_at(&lines, 0), "");
        assert_eq!(line_content_at(&lines, 3), "");
    }

    #[test]
    fn test_truncate_snippet_short_string() {
        assert_eq!(truncate_snippet("hello world", 80), "hello world");
    }

    #[test]
    fn test_truncate_snippet_collapses_whitespace() {
        assert_eq!(truncate_snippet("hello   \n\t  world", 80), "hello world");
    }

    #[test]
    fn test_truncate_snippet_truncates_long() {
        let long = "a".repeat(100);
        let result = truncate_snippet(&long, 10);
        assert_eq!(result, format!("{}...", "a".repeat(10)));
    }

    #[test]
    fn test_truncate_snippet_exact_length() {
        let s = "a".repeat(10);
        assert_eq!(truncate_snippet(&s, 10), s);
    }

    #[test]
    fn test_truncate_snippet_empty() {
        assert_eq!(truncate_snippet("", 10), "");
    }

    #[test]
    fn test_truncate_snippet_multibyte_utf8() {
        // Each CJK character is 3 bytes. "中文测试" = 12 bytes, 4 chars.
        // max_len=5 falls in the middle of a multibyte boundary — must not panic.
        let result = truncate_snippet("中文测试", 5);
        // The result must be valid UTF-8 (no panic) and end with "..."
        assert!(result.ends_with("..."), "expected '...' suffix, got: {}", result);
    }

    #[test]
    fn test_truncate_snippet_emoji() {
        // "hello 🌍 world" — emoji is 4 bytes; max_len=8 cuts inside the emoji
        let result = truncate_snippet("hello 🌍 world", 8);
        // Must not panic, must end with "..."
        assert!(result.ends_with("..."), "expected truncation with '...', got: {}", result);
    }
}
