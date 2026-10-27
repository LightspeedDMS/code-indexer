use super::*;

#[test]
fn macro_rules_definition_with_forbidden_path_body_is_rejected() {
    let source = r#"
macro_rules! innocuous_helper {
    () => {
        std::env::args().count();
    };
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    innocuous_helper!();
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "every macro_rules! definition must be rejected, whatever its body contains"
    );
}

#[test]
fn macro_rules_definition_with_unsafe_body_is_rejected() {
    let source = r#"
macro_rules! innocuous_helper {
    () => {
        unsafe {}
    };
}
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    innocuous_helper!();
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "every macro_rules! definition must be rejected, whatever its body contains"
    );
}

#[test]
fn non_allowlisted_macro_invocation_is_rejected() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    println!("{}", node.kind);
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "only vec!/format!/matches! may be invoked"
    );
}

#[test]
fn allowlisted_macros_vec_and_format_are_accepted() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let msg = format!("kind={}", node.kind);
    vec![EvalFinding { pattern: "x".to_string(), line: node.start_line, snippet: msg }]
}
"#;
    assert!(validate_evaluator_source(source).is_ok(), "vec!/format! must stay allowed");
}

#[test]
fn qualified_vec_macro_path_is_rejected() {
    // ^ why no `mod local` declaration, not a declared module; the `mod` ban would reject first and hide the path check
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _v: Vec<i32> = local::vec![1, 2, 3];
    Vec::new()
}
"#;
    let result = validate_evaluator_source(source);
    assert!(result.is_err(), "only a bare, unqualified vec!/format!/matches! is permitted");
    let errors = result.unwrap_err();
    assert!(
        !errors.iter().any(|e| e.message.contains("mod")),
        "the rejection must come from the qualified-path rule; got: {:?}",
        errors
    );
}

#[test]
fn format_macro_arguments_are_validated_like_code() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let msg = format!("{}", std::env::args().count());
    vec![EvalFinding { pattern: "x".to_string(), line: node.start_line, snippet: msg }]
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "format! arguments obey the same rules as code outside the macro"
    );
}

#[test]
fn vec_macro_arguments_are_validated_like_code() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _x = vec![std::env::args().count() as i32];
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "vec! arguments obey the same rules as code outside the macro"
    );
}

#[test]
fn vec_repeat_count_is_validated_like_code() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _x: Vec<i32> = vec![0; std::env::args().count()];
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "the count of vec![expr; count] obeys the same rules as the element"
    );
}

#[test]
fn matches_macro_is_allowlisted_and_accepted() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = matches!(node.kind.as_str(), "x" | "y" if node.start_line > 0);
    Vec::new()
}
"#;
    assert!(validate_evaluator_source(source).is_ok(), "matches! must stay allowed");
}

#[test]
fn matches_macro_guard_is_validated_like_code() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = matches!(node.kind.as_str(), _ if std::env::args().count() > 0);
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "the matches! guard obeys the same rules as code outside the macro"
    );
}

#[test]
fn matches_macro_scrutinee_is_validated_like_code() {
    let source = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let _ = matches!(std::env::args().count(), 0);
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "the matches! scrutinee obeys the same rules as code outside the macro"
    );
}

#[test]
fn no_mangle_attribute_is_rejected() {
    let source = r#"
#[no_mangle]
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(source).is_err(),
        "only doc attributes are permitted; exported symbol names stay under the harness's control"
    );
}

#[test]
fn no_std_inner_attribute_is_rejected() {
    let source = r#"
#![no_std]
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
    assert!(validate_evaluator_source(source).is_err(), "only doc attributes are permitted");
}

#[test]
fn doc_comment_attributes_are_accepted() {
    let source = r#"
/// Finds try-catch-rethrow patterns.
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
    assert!(validate_evaluator_source(source).is_ok(), "doc comments must stay allowed");
}

// ^ invariant TEST_NESTING_BEYOND_LIMIT > MAX_MACRO_RECURSION_DEPTH and < MAX_BRACKET_DEPTH + 8; just over the limit, never far
const TEST_NESTING_BEYOND_LIMIT: usize = 70;
const TEST_NESTING_WITHIN_LIMIT: usize = 5;

fn nested_vec_macro_source(nesting: usize) -> String {
    let nested = format!("{}1{}", "vec![".repeat(nesting), "]".repeat(nesting));
    format!(
        "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{\n    let _x = {};\n    Vec::new()\n}}\n",
        nested
    )
}

#[test]
fn deeply_nested_macros_beyond_the_recursion_limit_are_rejected() {
    let source = nested_vec_macro_source(TEST_NESTING_BEYOND_LIMIT);
    let result = validate_evaluator_source(&source);
    assert!(result.is_err(), "macro nesting beyond the depth limit must be rejected");
    let errors = result.unwrap_err();
    assert!(
        errors.iter().any(|e| e.message.to_lowercase().contains("depth")
            || e.message.to_lowercase().contains("recursion")
            || e.message.to_lowercase().contains("nest")),
        "the rejection must name the nesting limit; got: {:?}",
        errors
    );
}

#[test]
fn modestly_nested_macros_within_the_limit_are_accepted() {
    let source = nested_vec_macro_source(TEST_NESTING_WITHIN_LIMIT);
    assert!(validate_evaluator_source(&source).is_ok(), "modest macro nesting must stay accepted");
}

// ^ why literals 32 and 33, not MAX_MACRO_RECURSION_DEPTH; the test pins the value, so it must not follow a change
#[test]
fn macro_recursion_depth_boundary_pins_the_intended_limit_value() {
    let at_limit = nested_vec_macro_source(32);
    assert!(
        validate_evaluator_source(&at_limit).is_ok(),
        "macro nesting at exactly the intended limit (32) must be accepted"
    );

    let one_over_limit = nested_vec_macro_source(33);
    let errors = validate_evaluator_source(&one_over_limit)
        .expect_err("macro nesting one level past the intended limit (33) must be rejected");
    assert!(
        errors.iter().any(|e| e.message == "macro nesting exceeds the maximum depth of 32"),
        "{errors:?}"
    );
}

#[test]
fn validate_rust_graph_evaluator_accepts_valid_graph_source_and_rejects_forbidden_constructs_and_wrong_mode() {
    let valid_graph = r#"
fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult {
    GraphResult::default()
}
"#;
    assert!(validate_rust_graph_evaluator(valid_graph).is_ok(), "valid graph-mode source must pass");

    let graph_with_unsafe = r#"
fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> {
    unsafe { Vec::new() }
}
fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult {
    GraphResult::default()
}
"#;
    let result = validate_rust_graph_evaluator(graph_with_unsafe);
    assert!(result.is_err(), "graph-mode source with `unsafe` must still be rejected -- bans are unchanged");
    assert!(result.unwrap_err().iter().any(|e| e.message.contains("unsafe")));

    let legacy_only = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
    assert!(
        validate_rust_graph_evaluator(legacy_only).is_err(),
        "a legacy-shaped evaluator must not be accepted as graph mode"
    );

    let neither = "fn helper() -> i32 { 42 }";
    assert!(validate_rust_graph_evaluator(neither).is_err(), "source with no recognized callback family must be rejected");
}

#[test]
fn test_valid_code_passes() {
    let code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    if node.kind == "try_statement" {
        findings.push(EvalFinding {
            pattern: "test".to_string(),
            line: node.start_line,
            snippet: String::new(),
        });
    }
    findings
}
"#;
    assert!(validate_evaluator_source(code).is_ok());
}

fn assert_rejected_mentioning(code: &str, fragment: &str) {
    let result = validate_evaluator_source(code);
    assert!(result.is_err(), "must be rejected:\n{code}");
    let errors = result.unwrap_err();
    assert!(
        errors.iter().any(|e| e.message.contains(fragment)),
        "error must mention {fragment:?}, got: {:?}",
        errors
    );
}

#[test]
fn test_rejects_unsafe_block() {
    assert_rejected_mentioning("fn foo() {\n    unsafe { let x = 1; }\n}\n", "unsafe");
}

#[test]
fn test_rejects_unsafe_fn() {
    assert_rejected_mentioning("unsafe fn helper() {}", "unsafe");
}

#[test]
fn test_rejects_std_fs_import() {
    assert_rejected_mentioning("use std::fs::File;", "std::fs");
}

#[test]
fn test_rejects_std_net_import() {
    assert_rejected_mentioning("use std::net::TcpStream;", "std::net");
}

#[test]
fn test_rejects_std_process_import() {
    assert_rejected_mentioning("use std::process::id;", "std::process");
}

#[test]
fn test_rejects_raw_pointer_const() {
    assert_rejected_mentioning("fn foo(p: *const u8) {}", "*const");
}

#[test]
fn test_rejects_raw_pointer_mut() {
    assert_rejected_mentioning("fn foo(p: *mut u8) {}", "*mut");
}

#[test]
fn test_rejects_extern_block() {
    let code = r#"extern "C" { fn host_fn(); }"#;
    let result = validate_evaluator_source(code);
    assert!(result.is_err());
    assert!(!result.unwrap_err().is_empty());
}

#[test]
fn test_rejects_mod_declaration() {
    assert_rejected_mentioning("mod helpers { fn helper() {} }", "`mod`");
}

#[test]
fn test_rejects_include_macro() {
    assert_rejected_mentioning(r#"fn foo() { include!("sample.rs"); }"#, "include");
}

#[test]
fn test_rejects_env_macro() {
    assert_rejected_mentioning(r#"fn foo() -> &'static str { env!("PATH") }"#, "env");
}

#[test]
fn test_rejects_option_env_macro() {
    assert_rejected_mentioning(r#"fn foo() { let _ = option_env!("EXAMPLE"); }"#, "option_env");
}

#[test]
fn test_rejects_include_str_macro() {
    assert_rejected_mentioning(r#"const DATA: &str = include_str!("data.txt");"#, "include_str");
}

#[test]
fn test_error_contains_line_number() {
    let code = "fn foo() {\n    let x = 1;\n    unsafe { let _ = x; }\n}";
    let result = validate_evaluator_source(code);
    assert!(result.is_err());
    let errors = result.unwrap_err();
    assert!(!errors.is_empty());
    assert!(errors[0].line > 0);
    assert!(errors[0].to_string().contains("Line"));
}

#[test]
fn test_multiple_violations_reported() {
    let result = validate_evaluator_source("use std::fs; use std::net;");
    assert!(result.is_err());
    assert!(result.unwrap_err().len() >= 2);
}

#[test]
fn test_allowed_std_imports_pass() {
    let code = r#"
use std::collections::HashMap;
use std::fmt;
fn foo() {}
"#;
    assert!(validate_evaluator_source(code).is_ok());
}

#[test]
fn test_rejects_direct_std_fs_call() {
    assert_rejected_mentioning(r#"fn f() { let _ = std::fs::item(); }"#, "std::fs");
}

#[test]
fn test_rejects_direct_std_env_call() {
    assert_rejected_mentioning(r#"fn f() { let _ = std::env::var("X"); }"#, "std::env");
}

#[test]
fn test_rejects_direct_std_io() {
    assert_rejected_mentioning(r#"fn f() { let _ = std::io::stdin(); }"#, "std::io");
}

#[test]
fn test_allowed_std_collections_path() {
    let code = r#"fn f() { let _: std::collections::HashMap<i32, i32> = std::collections::HashMap::new(); }"#;
    let result = validate_evaluator_source(code);
    assert!(result.is_ok(), "std::collections path must be allowed, got: {:?}", result.err());
}

#[test]
fn test_rejects_println_macro() {
    assert_rejected_mentioning(r#"fn f() { println!("hi"); }"#, "println");
}

#[test]
fn test_rejects_eprintln_macro() {
    assert_rejected_mentioning(r#"fn f() { eprintln!("hi"); }"#, "eprintln");
}

#[test]
fn test_rejects_static_mut() {
    assert_rejected_mentioning("static mut X: i32 = 0; fn f() {}", "static");
}

#[test]
fn test_rejects_static_non_const() {
    assert_rejected_mentioning("static X: i32 = 0; fn f() {}", "static");
}

#[test]
fn test_allows_const() {
    let result = validate_evaluator_source("const X: i32 = 42; fn f() {}");
    assert!(result.is_ok(), "const declaration must be allowed, got: {:?}", result.err());
}
