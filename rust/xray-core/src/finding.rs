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

/// Collapse runs of whitespace in `s`, then truncate to `max_len` characters.
/// If truncation occurs, append "..." to the result.
pub fn truncate_snippet(s: &str, max_len: usize) -> String {
    let collapsed: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.len() <= max_len {
        collapsed
    } else {
        format!("{}...", &collapsed[..max_len])
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
}
