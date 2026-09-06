//! S0a per-reject-site regression floor for `validator.rs` (story #1789, AC2).
//!
//! `validator.rs` is the compiler-side half of the evaluator security boundary.
//! Its unit-test module covers the rules in aggregate; this file covers them
//! ONE CONSTRUCT PER TEST so that a regression names the exact rule that broke
//! instead of failing a single omnibus assertion.
//!
//! Three layers, deliberately:
//!
//! 1. `reject_site_tests!` — one `#[test]` per construct, asserting both that
//!    the construct is rejected AND that the rejection carries that site's own
//!    message (so merging two rules into one, or misrouting a construct to a
//!    neighbouring rule, still fails).
//! 2. `every_authoritative_reject_site_has_a_dedicated_test` — the meta-test.
//!    It keys on STABLE CONSTRUCT IDENTITY, never on counts and never on a
//!    test-name convention: the expected id set is COMPUTED from the rule
//!    tables below, and the covered id set is derived from the very macro
//!    invocation that defines the tests, so the two cannot drift apart
//!    silently.
//! 3. `rule_tables_match_validator_source` — the drift guard. The rule tables
//!    in this file are a mirror of literal lists inside `validator.rs`; this
//!    test re-extracts those lists from the real source and fails if the
//!    mirror is stale. Without it, adding a forbidden macro to `validator.rs`
//!    would leave the meta-test happily green with no test for the new site.

use xray_core::validator::validate_evaluator_source;

// ---------------------------------------------------------------------------
// Authoritative rule tables (mirrored from validator.rs, drift-guarded below)
// ---------------------------------------------------------------------------

/// std modules rejected by `visit_path` and by `check_forbidden_std_subpath`.
const FORBIDDEN_STD_MODULES: &[&str] = &["fs", "net", "process", "env", "io"];

/// Syntactic forms in which a forbidden std module can appear. Each form is a
/// SEPARATE reject site in validator.rs, so each needs its own test.
const STD_MODULE_FORMS: &[&str] = &["path_expr", "use_path", "use_name", "use_rename"];

/// Macros rejected by `visit_macro`.
const FORBIDDEN_MACROS: &[&str] = &[
    "include",
    "include_str",
    "include_bytes",
    "env",
    "option_env",
    "println",
    "eprintln",
    "print",
    "eprint",
    "panic",
    "todo",
    "unimplemented",
];

/// Reject sites that are not part of a family cross-product.
const SINGLETON_CONSTRUCTS: &[&str] = &[
    "unsafe_block",
    "unsafe_fn",
    "static_decl",
    "static_mut_decl",
    "raw_ptr_const",
    "raw_ptr_mut",
    "extern_block",
    "extern_abi_fn",
    "mod_decl",
    "use_std_glob",
    "syntax_error",
];

// ---------------------------------------------------------------------------
// Shared assertion
// ---------------------------------------------------------------------------

/// Asserts `source` is rejected and that some error carries `expected_fragment`.
///
/// The fragment is the site's own distinguishing wording, so a construct that
/// gets rejected by the WRONG rule still fails this assertion.
fn assert_reject_site(construct_id: &str, source: &str, expected_fragment: &str) {
    let result = validate_evaluator_source(source);
    let errors = match result {
        Ok(()) => panic!(
            "reject site '{construct_id}' ACCEPTED forbidden source:\n{source}\n\
             The validator no longer rejects this construct."
        ),
        Err(errors) => errors,
    };
    assert!(
        errors.iter().any(|e| e.message.contains(expected_fragment)),
        "reject site '{construct_id}' was rejected, but by the wrong rule.\n\
         source:\n{source}\nexpected message fragment: {expected_fragment:?}\n\
         actual messages: {:?}",
        errors.iter().map(|e| &e.message).collect::<Vec<_>>()
    );
    assert!(
        errors.iter().all(|e| e.line > 0),
        "reject site '{construct_id}' produced a non-positive line number: {:?}",
        errors.iter().map(|e| e.line).collect::<Vec<_>>()
    );
}

// ---------------------------------------------------------------------------
// Per-reject-site tests
// ---------------------------------------------------------------------------

macro_rules! reject_site_tests {
    ($( $test_name:ident : $id:expr => $src:expr, $frag:expr ; )*) => {
        $(
            #[test]
            fn $test_name() {
                assert_reject_site($id, $src, $frag);
            }
        )*

        /// Construct ids that actually own a dedicated `#[test]` above.
        ///
        /// Derived from the SAME macro invocation that generates the tests, so
        /// it is structurally impossible for a listed id to lack a test.
        const COVERED_CONSTRUCT_IDS: &[&str] = &[$($id),*];
    };
}

reject_site_tests! {
    // ---- singleton rules ----
    rejects_unsafe_block: "unsafe_block"
        => "fn f() { unsafe { let _ = 1; } }",
           "`unsafe` blocks are not allowed";
    rejects_unsafe_fn: "unsafe_fn"
        => "unsafe fn f() {}",
           "`unsafe` functions are not allowed";
    rejects_static_declaration: "static_decl"
        => "static X: i32 = 0;",
           "`static` declarations are not allowed";
    rejects_static_mut_declaration: "static_mut_decl"
        => "static mut X: i32 = 0;",
           "`static` declarations are not allowed";
    rejects_raw_pointer_const: "raw_ptr_const"
        => "fn f(p: *const u8) {}",
           "Raw pointer type `*const`";
    rejects_raw_pointer_mut: "raw_ptr_mut"
        => "fn f(p: *mut u8) {}",
           "Raw pointer type `*mut`";
    rejects_extern_block: "extern_block"
        => r#"extern "C" { fn g(); }"#,
           "`extern` blocks are not allowed";
    rejects_extern_abi_fn: "extern_abi_fn"
        => r#"extern "C" fn f() {}"#,
           "`extern` ABI functions are not allowed";
    rejects_mod_declaration: "mod_decl"
        => "mod m { fn g() {} }",
           "`mod` declarations are not allowed";
    rejects_use_std_glob: "use_std_glob"
        => "use std::*;",
           "`use std::*` (glob import) is not allowed";
    rejects_unparseable_source: "syntax_error"
        => "fn f( {",
           "Syntax error:";

    // ---- forbidden std modules: fully-qualified path expression ----
    rejects_std_fs_path_expr: "std_module:fs:path_expr"
        => r#"fn f() { let _ = std::fs::read_to_string("x"); }"#,
           "`std::fs` is not allowed";
    rejects_std_net_path_expr: "std_module:net:path_expr"
        => r#"fn f() { let _ = std::net::TcpStream::connect("x"); }"#,
           "`std::net` is not allowed";
    rejects_std_process_path_expr: "std_module:process:path_expr"
        => "fn f() { std::process::exit(0); }",
           "`std::process` is not allowed";
    rejects_std_env_path_expr: "std_module:env:path_expr"
        => r#"fn f() { let _ = std::env::var("X"); }"#,
           "`std::env` is not allowed";
    rejects_std_io_path_expr: "std_module:io:path_expr"
        => "fn f() { let _ = std::io::stdin(); }",
           "`std::io` is not allowed";

    // ---- forbidden std modules: `use std::<mod>::<item>` (UseTree::Path) ----
    rejects_std_fs_use_path: "std_module:fs:use_path"
        => "use std::fs::File;",
           "`use std::fs` (or sub-path) is not allowed";
    rejects_std_net_use_path: "std_module:net:use_path"
        => "use std::net::TcpStream;",
           "`use std::net` (or sub-path) is not allowed";
    rejects_std_process_use_path: "std_module:process:use_path"
        => "use std::process::Command;",
           "`use std::process` (or sub-path) is not allowed";
    rejects_std_env_use_path: "std_module:env:use_path"
        => "use std::env::vars;",
           "`use std::env` (or sub-path) is not allowed";
    rejects_std_io_use_path: "std_module:io:use_path"
        => "use std::io::Read;",
           "`use std::io` (or sub-path) is not allowed";

    // ---- forbidden std modules: `use std::<mod>;` (UseTree::Name) ----
    rejects_std_fs_use_name: "std_module:fs:use_name"
        => "use std::fs;",
           "`use std::fs` is not allowed";
    rejects_std_net_use_name: "std_module:net:use_name"
        => "use std::net;",
           "`use std::net` is not allowed";
    rejects_std_process_use_name: "std_module:process:use_name"
        => "use std::process;",
           "`use std::process` is not allowed";
    rejects_std_env_use_name: "std_module:env:use_name"
        => "use std::env;",
           "`use std::env` is not allowed";
    rejects_std_io_use_name: "std_module:io:use_name"
        => "use std::io;",
           "`use std::io` is not allowed";

    // ---- forbidden std modules: `use std::<mod> as x;` (UseTree::Rename) ----
    rejects_std_fs_use_rename: "std_module:fs:use_rename"
        => "use std::fs as filesystem;",
           "`use std::fs` (renamed) is not allowed";
    rejects_std_net_use_rename: "std_module:net:use_rename"
        => "use std::net as network;",
           "`use std::net` (renamed) is not allowed";
    rejects_std_process_use_rename: "std_module:process:use_rename"
        => "use std::process as proc_mod;",
           "`use std::process` (renamed) is not allowed";
    rejects_std_env_use_rename: "std_module:env:use_rename"
        => "use std::env as environment;",
           "`use std::env` (renamed) is not allowed";
    rejects_std_io_use_rename: "std_module:io:use_rename"
        => "use std::io as stdio;",
           "`use std::io` (renamed) is not allowed";

    // ---- forbidden macros ----
    rejects_include_macro: "macro:include"
        => r#"fn f() { include!("evil.rs"); }"#,
           "`include!` macro is not allowed";
    rejects_include_str_macro: "macro:include_str"
        => r#"const D: &str = include_str!("secret.txt");"#,
           "`include_str!` macro is not allowed";
    rejects_include_bytes_macro: "macro:include_bytes"
        => r#"const D: &[u8] = include_bytes!("secret.bin");"#,
           "`include_bytes!` macro is not allowed";
    rejects_env_macro: "macro:env"
        => r#"fn f() -> &'static str { env!("PATH") }"#,
           "`env!` macro is not allowed";
    rejects_option_env_macro: "macro:option_env"
        => r#"fn f() { let _ = option_env!("SECRET"); }"#,
           "`option_env!` macro is not allowed";
    rejects_println_macro: "macro:println"
        => r#"fn f() { println!("hi"); }"#,
           "`println!` macro is not allowed";
    rejects_eprintln_macro: "macro:eprintln"
        => r#"fn f() { eprintln!("hi"); }"#,
           "`eprintln!` macro is not allowed";
    rejects_print_macro: "macro:print"
        => r#"fn f() { print!("hi"); }"#,
           "`print!` macro is not allowed";
    rejects_eprint_macro: "macro:eprint"
        => r#"fn f() { eprint!("hi"); }"#,
           "`eprint!` macro is not allowed";
    rejects_panic_macro: "macro:panic"
        => r#"fn f() { panic!("boom"); }"#,
           "`panic!` macro is not allowed";
    rejects_todo_macro: "macro:todo"
        => "fn f() { todo!(); }",
           "`todo!` macro is not allowed";
    rejects_unimplemented_macro: "macro:unimplemented"
        => "fn f() { unimplemented!(); }",
           "`unimplemented!` macro is not allowed";
}

// ---------------------------------------------------------------------------
// Meta-test: every authoritative reject site owns a dedicated test
// ---------------------------------------------------------------------------

/// Builds the construct-id set the validator's rules imply.
///
/// Computed from the rule tables (never hand-listed alongside the tests), so
/// extending a rule family automatically extends what must be covered.
fn expected_construct_ids() -> Vec<String> {
    let mut ids: Vec<String> = SINGLETON_CONSTRUCTS.iter().map(|s| s.to_string()).collect();
    for module in FORBIDDEN_STD_MODULES {
        for form in STD_MODULE_FORMS {
            ids.push(format!("std_module:{module}:{form}"));
        }
    }
    for name in FORBIDDEN_MACROS {
        ids.push(format!("macro:{name}"));
    }
    ids.sort();
    ids
}

#[test]
fn every_authoritative_reject_site_has_a_dedicated_test() {
    let expected = expected_construct_ids();
    let mut covered: Vec<String> = COVERED_CONSTRUCT_IDS.iter().map(|s| s.to_string()).collect();
    covered.sort();

    let missing: Vec<&String> = expected.iter().filter(|id| !covered.contains(id)).collect();
    assert!(
        missing.is_empty(),
        "these validator reject sites have NO dedicated test: {missing:?}\n\
         Add one `reject_site_tests!` entry per id above."
    );

    let stray: Vec<&String> = covered.iter().filter(|id| !expected.contains(id)).collect();
    assert!(
        stray.is_empty(),
        "these tests claim construct ids the rule tables do not define: {stray:?}\n\
         Either fix the id or add the construct to the rule tables."
    );
}

#[test]
fn covered_construct_ids_are_unique() {
    // Two tests sharing one id would let a real site hide behind a duplicate.
    let mut seen: Vec<&str> = COVERED_CONSTRUCT_IDS.to_vec();
    let before = seen.len();
    seen.sort_unstable();
    seen.dedup();
    assert_eq!(
        before,
        seen.len(),
        "duplicate construct ids in reject_site_tests!: {:?}",
        COVERED_CONSTRUCT_IDS
    );
}

// ---------------------------------------------------------------------------
// Drift guard: the rule tables above must mirror validator.rs itself
// ---------------------------------------------------------------------------

const VALIDATOR_SOURCE: &str = include_str!("../src/validator.rs");

/// True for a string literal that looks like a bare lowercase Rust identifier
/// (the shape every module/macro name in validator.rs's `matches!` arms has).
/// Message literals contain backticks, spaces and `{}` and are filtered out.
fn is_ident_like(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
}

/// Collects identifier-shaped string literals from the source slice bounded by
/// `start_marker` and the following `end_marker`.
fn ident_literals_between(start_marker: &str, end_marker: &str) -> Vec<String> {
    let start = VALIDATOR_SOURCE
        .find(start_marker)
        .unwrap_or_else(|| panic!("validator.rs no longer contains {start_marker:?}"));
    let rest = &VALIDATOR_SOURCE[start..];
    let end = rest
        .find(end_marker)
        .unwrap_or_else(|| panic!("validator.rs no longer contains {end_marker:?} after {start_marker:?}"));
    let slice = &rest[..end];

    let mut found: Vec<String> = slice
        .split('"')
        .skip(1)
        .step_by(2)
        .filter(|lit| is_ident_like(lit))
        .map(|lit| lit.to_string())
        .collect();
    found.sort();
    found.dedup();
    found
}

#[test]
fn rule_tables_match_validator_source() {
    // Forbidden macros, as literally listed in `visit_macro`.
    let mut expected_macros: Vec<String> =
        FORBIDDEN_MACROS.iter().map(|s| s.to_string()).collect();
    expected_macros.sort();
    let actual_macros = ident_literals_between("fn visit_macro", "syn::visit::visit_macro");
    assert_eq!(
        actual_macros, expected_macros,
        "FORBIDDEN_MACROS is out of sync with validator.rs's visit_macro. \
         Update the table AND add a reject_site_tests! entry for each new macro."
    );

    // Forbidden std modules, as literally listed in `visit_path`. "std" is the
    // path root, not a forbidden module, so it is excluded from the comparison.
    let mut expected_modules: Vec<String> =
        FORBIDDEN_STD_MODULES.iter().map(|s| s.to_string()).collect();
    expected_modules.sort();
    let actual_path_modules: Vec<String> =
        ident_literals_between("fn visit_path", "syn::visit::visit_path")
            .into_iter()
            .filter(|s| s != "std")
            .collect();
    assert_eq!(
        actual_path_modules, expected_modules,
        "FORBIDDEN_STD_MODULES is out of sync with validator.rs's visit_path."
    );

    // The same module list is repeated in check_forbidden_std_subpath for the
    // `use` forms; it must agree with the path-expression list.
    let actual_use_modules =
        ident_literals_between("fn check_forbidden_std_subpath", "#[cfg(test)]");
    assert_eq!(
        actual_use_modules, expected_modules,
        "FORBIDDEN_STD_MODULES is out of sync with check_forbidden_std_subpath."
    );
}

// ---------------------------------------------------------------------------
// Traversal behaviour that is not itself a reject site
// ---------------------------------------------------------------------------

#[test]
fn group_use_reaches_every_member_reject_site() {
    // `UseTree::Group` pushes no error of its own — it recurses. A regression
    // that stopped recursing would silently admit `use std::{fs, net};`.
    let result = validate_evaluator_source("use std::{fs, net, collections};");
    let errors = result.expect_err("grouped forbidden imports must be rejected");
    for module in ["fs", "net"] {
        assert!(
            errors
                .iter()
                .any(|e| e.message.contains(&format!("`use std::{module}`"))),
            "grouped import did not reject std::{module}; got {:?}",
            errors.iter().map(|e| &e.message).collect::<Vec<_>>()
        );
    }
    assert!(
        !errors.iter().any(|e| e.message.contains("collections")),
        "allowed module in a group must not be rejected: {:?}",
        errors.iter().map(|e| &e.message).collect::<Vec<_>>()
    );
}

#[test]
fn every_violation_in_a_file_is_reported_not_just_the_first() {
    let errors = validate_evaluator_source("use std::fs;\nuse std::net;\nstatic X: i32 = 0;")
        .expect_err("multiple violations must be rejected");
    assert!(
        errors.len() >= 3,
        "validator must report every violation, got {:?}",
        errors.iter().map(|e| &e.message).collect::<Vec<_>>()
    );
}

#[test]
fn allowed_constructs_are_not_rejected_by_any_site() {
    // Negative control: without this, a validator that rejected EVERYTHING
    // would pass every test above.
    let code = r#"
use std::collections::HashMap;
use std::fmt;
const LIMIT: usize = 42;
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    *counts.entry(node.kind.clone()).or_insert(0) += 1;
    let _ = LIMIT;
    let _ = format!("{:?}", node.start_line);
    Vec::new()
}
"#;
    assert!(
        validate_evaluator_source(code).is_ok(),
        "allowed evaluator code must pass: {:?}",
        validate_evaluator_source(code).err()
    );
}
