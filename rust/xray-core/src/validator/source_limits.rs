use super::ValidationError;

// ^ invariant MAX_EVALUATOR_SOURCE_BYTES ~ 18x largest documented evaluator (7117 bytes, docs/guides/xray-cookbook.md)
// ^ coupled MAX_EVALUATOR_SOURCE_BYTES, MAX_BRACKET_DEPTH with src/code_indexer/xray/rust_source_limits.py; test:python_mirror_matches_source_limits
pub const MAX_EVALUATOR_SOURCE_BYTES: usize = 131_072;
// ^ why fixed depth limit, not a larger one; deeper source gets a validation error before syn parses it
// ^ invariant MAX_BRACKET_DEPTH > 2 * visit_macro MAX_MACRO_RECURSION_DEPTH; deepest documented evaluator = 9
pub const MAX_BRACKET_DEPTH: usize = 64;

// ^ order check_source_limits before syn::parse_str; over-limit source => one ValidationError, never parsed
pub(super) fn check_source_limits(source: &str) -> Option<ValidationError> {
    if source.len() > MAX_EVALUATOR_SOURCE_BYTES {
        return Some(ValidationError {
            line: 1,
            message: format!(
                "evaluator source is {} bytes, over the maximum of {} bytes",
                source.len(),
                MAX_EVALUATOR_SOURCE_BYTES
            ),
        });
    }
    let mut depth = 0usize;
    let mut line = 1usize;
    for byte in source.bytes() {
        match byte {
            b'\n' => line += 1,
            b'{' | b'(' | b'[' => {
                depth += 1;
                if depth > MAX_BRACKET_DEPTH {
                    return Some(ValidationError {
                        line,
                        message: format!(
                            "bracket nesting depth exceeds the maximum of {} (`{{`, `(` and `[` counted together)",
                            MAX_BRACKET_DEPTH
                        ),
                    });
                }
            }
            b'}' | b')' | b']' => depth = depth.saturating_sub(1),
            _ => {}
        }
    }
    None
}
