use super::validate_evaluator_source;

// ^ why literals 131_072 and 64, not the constants; these tests pin the values, so they must not follow a change
const PINNED_MAX_SOURCE_BYTES: usize = 131_072;
const PINNED_MAX_BRACKET_DEPTH: usize = 64;

const ENTRY: &str = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }\n";

fn source_of_exact_size(bytes: usize) -> String {
    let mut source = format!("{ENTRY}// ");
    source.push_str(&"x".repeat(bytes - source.len()));
    assert_eq!(source.len(), bytes);
    source
}

// ^ invariant nested_source(d) has maximum bracket depth = d; fn body brace = 1, rest cycle `(`, `[`, `{`
fn nested_source(depth: usize) -> String {
    let opens = ["(", "[", "{"];
    let closes = [")", "]", "}"];
    let inner = depth - 1;
    let open: String = (0..inner).map(|i| opens[i % 3]).collect();
    let close: String = (0..inner).rev().map(|i| closes[i % 3]).collect();
    format!("{ENTRY}fn f() {{\n    let _ = {open}1{close};\n}}\n")
}

fn messages(source: &str) -> Vec<String> {
    validate_evaluator_source(source)
        .expect_err("source must be rejected")
        .into_iter()
        .map(|e| e.message)
        .collect()
}

#[test]
fn source_at_the_size_limit_is_judged_normally() {
    let source = source_of_exact_size(PINNED_MAX_SOURCE_BYTES);
    assert!(validate_evaluator_source(&source).is_ok(), "{:?}", validate_evaluator_source(&source).err());
}

#[test]
fn source_one_byte_over_the_size_limit_is_rejected_before_parsing() {
    let source = source_of_exact_size(PINNED_MAX_SOURCE_BYTES + 1);
    let errors = validate_evaluator_source(&source).expect_err("oversized source must be rejected");
    assert_eq!(errors.len(), 1, "{errors:?}");
    assert_eq!(errors[0].line, 1);
    assert!(
        errors[0].message.contains("131073 bytes") && errors[0].message.contains("maximum of 131072 bytes"),
        "{errors:?}"
    );
}

#[test]
fn size_limit_rejects_source_that_would_otherwise_be_a_syntax_error() {
    let mut source = "fn f( {".to_string();
    source.push_str(&" ".repeat(PINNED_MAX_SOURCE_BYTES));
    let found = messages(&source);
    assert!(found.iter().all(|m| !m.starts_with("Syntax error")), "limit must be checked before parsing: {found:?}");
}

#[test]
fn bracket_depth_at_the_limit_is_judged_normally() {
    let source = nested_source(PINNED_MAX_BRACKET_DEPTH);
    assert!(validate_evaluator_source(&source).is_ok(), "{:?}", validate_evaluator_source(&source).err());
}

#[test]
fn bracket_depth_one_over_the_limit_is_rejected_before_parsing() {
    let source = nested_source(PINNED_MAX_BRACKET_DEPTH + 1);
    let errors = validate_evaluator_source(&source).expect_err("too-deep nesting must be rejected");
    assert_eq!(errors.len(), 1, "{errors:?}");
    assert_eq!(errors[0].line, 3, "the depth error names the line where the limit is crossed");
    assert!(errors[0].message.contains("nesting depth exceeds the maximum of 64"), "{errors:?}");
}

#[test]
fn bracket_depth_counts_unbalanced_source_before_parsing() {
    let source = format!("{ENTRY}{}", "(".repeat(PINNED_MAX_BRACKET_DEPTH + 1));
    let found = messages(&source);
    assert!(
        found.iter().any(|m| m.contains("nesting depth exceeds")) && found.iter().all(|m| !m.starts_with("Syntax error")),
        "{found:?}"
    );
}

const PYTHON_MIRROR: &str = include_str!("../../../../src/code_indexer/xray/rust_source_limits.py");

fn python_int_constant(name: &str) -> usize {
    let assignment = format!("{name} = ");
    let start = PYTHON_MIRROR
        .lines()
        .find_map(|line| line.strip_prefix(&assignment))
        .unwrap_or_else(|| panic!("rust_source_limits.py no longer assigns {name}"));
    start.trim().replace('_', "").parse().unwrap_or_else(|_| panic!("{name} is not an integer literal: {start}"))
}

#[test]
fn python_mirror_matches_source_limits() {
    assert_eq!(python_int_constant("MAX_EVALUATOR_SOURCE_BYTES"), super::source_limits::MAX_EVALUATOR_SOURCE_BYTES);
    assert_eq!(python_int_constant("MAX_BRACKET_DEPTH"), super::source_limits::MAX_BRACKET_DEPTH);
}

#[test]
fn closing_brackets_lower_the_depth() {
    let flat = format!("{ENTRY}fn f() {{ {} }}\n", "let _ = (1);".repeat(PINNED_MAX_BRACKET_DEPTH * 2));
    assert!(validate_evaluator_source(&flat).is_ok(), "{:?}", validate_evaluator_source(&flat).err());
}
