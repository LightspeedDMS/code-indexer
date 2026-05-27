/// A finding produced by a scan, attached to a specific file path.
#[derive(Debug, Clone)]
pub struct Finding {
    pub pattern: String,
    pub file: String,
    pub line: usize,
    pub snippet: String,
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

#[cfg(test)]
mod tests {
    use super::*;

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
