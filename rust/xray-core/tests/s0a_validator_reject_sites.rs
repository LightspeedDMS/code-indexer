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

/// std modules outside the allowlist, each exercised in every form below.
const REJECTED_STD_MODULES: &[&str] = &["fs", "net", "process", "env", "io", "os", "thread", "ffi", "path"];

/// Syntactic forms in which a std path can appear: `visit_path` owns
/// path_expr, `std_paths::use_tree_violations` owns the three `use` forms.
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
    "unsafe_impl_method",
    "unsafe_trait_method",
    "unsafe_impl",
    "unsafe_trait",
    "static_decl",
    "static_mut_decl",
    "raw_ptr_const",
    "raw_ptr_mut",
    "extern_block",
    "extern_abi_fn",
    "extern_abi_fn_ptr",
    "unsafe_fn_ptr",
    "mod_decl",
    "use_std_glob",
    "use_std_root_rename",
    "extern_crate",
    "syntax_error",
    "source_too_large",
    "bracket_nesting_too_deep",
    "verbatim_item",
    "verbatim_impl_item",
    "verbatim_trait_item",
    "verbatim_foreign_item",
    "verbatim_expr",
    "verbatim_type",
    "verbatim_pat",
    "verbatim_type_param_bound",
    "macro_rules_definition",
    "macro_rules_definition_with_forbidden_body",
];

/// CRITICAL (Codex follow-up review; expanded R2-2 re-review): macro
/// invocations are a FAIL-CLOSED ALLOWLIST, not a blocklist -- `vec!`/
/// `format!`/`matches!` are the only macros this project's own shipped
/// evaluator examples (incl. the catch-rethrow seed pattern) use. Each
/// allowlisted macro's token stream is recursively parsed and validated
/// (see `validate_allowed_macro_tokens` in validator.rs) rather than
/// trusted blindly -- `matches!` was re-added here only once that content
/// inspection existed, never as a bare re-permit. Mirrored from
/// `visit_macro`'s `ALLOWED_MACROS` constant, drift-guarded below exactly
/// like `FORBIDDEN_STD_MODULES`/`FORBIDDEN_MACROS`.
const ALLOWED_MACROS: &[&str] = &["format", "matches", "vec"];

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
    rejects_unsafe_impl_method: "unsafe_impl_method"
        => "struct S; impl S { unsafe fn f() {} }",
           "`unsafe` functions are not allowed";
    rejects_unsafe_trait_method: "unsafe_trait_method"
        => "trait T { unsafe fn f(); }",
           "`unsafe` functions are not allowed";
    rejects_unsafe_impl: "unsafe_impl"
        => "struct S; unsafe impl Send for S {}",
           "`unsafe impl` blocks are not allowed";
    rejects_unsafe_trait: "unsafe_trait"
        => "unsafe trait T {}",
           "`unsafe trait` declarations are not allowed";
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
    rejects_extern_abi_fn_pointer_type: "extern_abi_fn_ptr"
        => r#"fn f(_: extern "C" fn()) {}"#,
           "`extern` ABI function pointer types are not allowed";
    rejects_unsafe_fn_pointer_type: "unsafe_fn_ptr"
        => "fn f(_: unsafe fn()) {}",
           "`unsafe` function pointer types are not allowed";
    rejects_mod_declaration: "mod_decl"
        => "mod m { fn g() {} }",
           "`mod` declarations are not allowed";
    rejects_oversized_source: "source_too_large"
        => &" ".repeat(131_073),
           "over the maximum of 131072 bytes";
    rejects_over_deep_bracket_nesting: "bracket_nesting_too_deep"
        => &"(".repeat(65),
           "bracket nesting depth exceeds the maximum of 64";
    rejects_verbatim_item: "verbatim_item"
        => "unsafe fn f();",
           "unparsed item syntax";
    rejects_verbatim_impl_item: "verbatim_impl_item"
        => "struct S; impl S { fn f(); }",
           "unparsed impl item syntax";
    rejects_verbatim_trait_item: "verbatim_trait_item"
        => "trait T { pub type A; }",
           "unparsed trait item syntax";
    rejects_verbatim_foreign_item: "verbatim_foreign_item"
        => r#"extern "C" { safe fn g(); }"#,
           "unparsed foreign item syntax";
    rejects_verbatim_expr: "verbatim_expr"
        => "fn f() { let _ = builtin # offset_of(S, a); }",
           "unparsed expression syntax";
    rejects_verbatim_type: "verbatim_type"
        => "fn f(_: dyn* Tr) {}",
           "unparsed type syntax";
    rejects_verbatim_pat: "verbatim_pat"
        => "fn f() { let box x = y; }",
           "unparsed pattern syntax";
    rejects_verbatim_type_param_bound: "verbatim_type_param_bound"
        => "fn f<T: const Tr>() {}",
           "unparsed type bound syntax";
    rejects_unparseable_source: "syntax_error"
        => "fn f( {",
           "Syntax error:";
    rejects_use_std_glob: "use_std_glob"
        => "use std::*;",
           "`use std::*` (glob import) is not a permitted standard-library import";
    rejects_use_std_root_rename: "use_std_root_rename"
        => "use std as s;",
           "`use std as s` renames a standard-library path";
    rejects_extern_crate: "extern_crate"
        => "extern crate std as s;",
           "`extern crate` declarations are not allowed";

    // ---- std modules outside the allowlist: path_expr ----
    rejects_std_fs_path_expr: "std_module:fs:path_expr"
        => "fn f() { let _ = std::fs::item; }",
           "`std::fs::item` is not a permitted standard-library path";
    rejects_std_net_path_expr: "std_module:net:path_expr"
        => "fn f() { let _ = std::net::item; }",
           "`std::net::item` is not a permitted standard-library path";
    rejects_std_process_path_expr: "std_module:process:path_expr"
        => "fn f() { let _ = std::process::item; }",
           "`std::process::item` is not a permitted standard-library path";
    rejects_std_env_path_expr: "std_module:env:path_expr"
        => "fn f() { let _ = std::env::item; }",
           "`std::env::item` is not a permitted standard-library path";
    rejects_std_io_path_expr: "std_module:io:path_expr"
        => "fn f() { let _ = std::io::item; }",
           "`std::io::item` is not a permitted standard-library path";
    rejects_std_os_path_expr: "std_module:os:path_expr"
        => "fn f() { let _ = std::os::item; }",
           "`std::os::item` is not a permitted standard-library path";
    rejects_std_thread_path_expr: "std_module:thread:path_expr"
        => "fn f() { let _ = std::thread::item; }",
           "`std::thread::item` is not a permitted standard-library path";
    rejects_std_ffi_path_expr: "std_module:ffi:path_expr"
        => "fn f() { let _ = std::ffi::item; }",
           "`std::ffi::item` is not a permitted standard-library path";
    rejects_std_path_path_expr: "std_module:path:path_expr"
        => "fn f() { let _ = std::path::item; }",
           "`std::path::item` is not a permitted standard-library path";

    // ---- std modules outside the allowlist: use_path ----
    rejects_std_fs_use_path: "std_module:fs:use_path"
        => "use std::fs::Item;",
           "`use std::fs::Item` is not a permitted standard-library import";
    rejects_std_net_use_path: "std_module:net:use_path"
        => "use std::net::Item;",
           "`use std::net::Item` is not a permitted standard-library import";
    rejects_std_process_use_path: "std_module:process:use_path"
        => "use std::process::Item;",
           "`use std::process::Item` is not a permitted standard-library import";
    rejects_std_env_use_path: "std_module:env:use_path"
        => "use std::env::Item;",
           "`use std::env::Item` is not a permitted standard-library import";
    rejects_std_io_use_path: "std_module:io:use_path"
        => "use std::io::Item;",
           "`use std::io::Item` is not a permitted standard-library import";
    rejects_std_os_use_path: "std_module:os:use_path"
        => "use std::os::Item;",
           "`use std::os::Item` is not a permitted standard-library import";
    rejects_std_thread_use_path: "std_module:thread:use_path"
        => "use std::thread::Item;",
           "`use std::thread::Item` is not a permitted standard-library import";
    rejects_std_ffi_use_path: "std_module:ffi:use_path"
        => "use std::ffi::Item;",
           "`use std::ffi::Item` is not a permitted standard-library import";
    rejects_std_path_use_path: "std_module:path:use_path"
        => "use std::path::Item;",
           "`use std::path::Item` is not a permitted standard-library import";

    // ---- std modules outside the allowlist: use_name ----
    rejects_std_fs_use_name: "std_module:fs:use_name"
        => "use std::fs;",
           "`use std::fs` is not a permitted standard-library import";
    rejects_std_net_use_name: "std_module:net:use_name"
        => "use std::net;",
           "`use std::net` is not a permitted standard-library import";
    rejects_std_process_use_name: "std_module:process:use_name"
        => "use std::process;",
           "`use std::process` is not a permitted standard-library import";
    rejects_std_env_use_name: "std_module:env:use_name"
        => "use std::env;",
           "`use std::env` is not a permitted standard-library import";
    rejects_std_io_use_name: "std_module:io:use_name"
        => "use std::io;",
           "`use std::io` is not a permitted standard-library import";
    rejects_std_os_use_name: "std_module:os:use_name"
        => "use std::os;",
           "`use std::os` is not a permitted standard-library import";
    rejects_std_thread_use_name: "std_module:thread:use_name"
        => "use std::thread;",
           "`use std::thread` is not a permitted standard-library import";
    rejects_std_ffi_use_name: "std_module:ffi:use_name"
        => "use std::ffi;",
           "`use std::ffi` is not a permitted standard-library import";
    rejects_std_path_use_name: "std_module:path:use_name"
        => "use std::path;",
           "`use std::path` is not a permitted standard-library import";

    // ---- std modules outside the allowlist: use_rename ----
    rejects_std_fs_use_rename: "std_module:fs:use_rename"
        => "use std::fs as renamed;",
           "`use std::fs as renamed` renames a standard-library path";
    rejects_std_net_use_rename: "std_module:net:use_rename"
        => "use std::net as renamed;",
           "`use std::net as renamed` renames a standard-library path";
    rejects_std_process_use_rename: "std_module:process:use_rename"
        => "use std::process as renamed;",
           "`use std::process as renamed` renames a standard-library path";
    rejects_std_env_use_rename: "std_module:env:use_rename"
        => "use std::env as renamed;",
           "`use std::env as renamed` renames a standard-library path";
    rejects_std_io_use_rename: "std_module:io:use_rename"
        => "use std::io as renamed;",
           "`use std::io as renamed` renames a standard-library path";
    rejects_std_os_use_rename: "std_module:os:use_rename"
        => "use std::os as renamed;",
           "`use std::os as renamed` renames a standard-library path";
    rejects_std_thread_use_rename: "std_module:thread:use_rename"
        => "use std::thread as renamed;",
           "`use std::thread as renamed` renames a standard-library path";
    rejects_std_ffi_use_rename: "std_module:ffi:use_rename"
        => "use std::ffi as renamed;",
           "`use std::ffi as renamed` renames a standard-library path";
    rejects_std_path_use_rename: "std_module:path:use_rename"
        => "use std::path as renamed;",
           "`use std::path as renamed` renames a standard-library path";

    // ---- forbidden macros ----
    rejects_include_macro: "macro:include"
        => r#"fn f() { include!("sample.rs"); }"#,
           "`include!` macro is not allowed";
    rejects_include_str_macro: "macro:include_str"
        => r#"const D: &str = include_str!("data.txt");"#,
           "`include_str!` macro is not allowed";
    rejects_include_bytes_macro: "macro:include_bytes"
        => r#"const D: &[u8] = include_bytes!("data.bin");"#,
           "`include_bytes!` macro is not allowed";
    rejects_env_macro: "macro:env"
        => r#"fn f() -> &'static str { env!("PATH") }"#,
           "`env!` macro is not allowed";
    rejects_option_env_macro: "macro:option_env"
        => r#"fn f() { let _ = option_env!("EXAMPLE"); }"#,
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

    // ---- macro definitions ----
    // ^ invariant every macro_rules! definition => rejected, whatever its body; no other site sees inside the body
    rejects_macro_rules_definition: "macro_rules_definition"
        => r#"macro_rules! harmless_name { () => { 1 + 1 }; } fn f() { harmless_name!(); }"#,
           "`macro_rules!` definitions are not allowed";
    rejects_macro_rules_definition_with_forbidden_body: "macro_rules_definition_with_forbidden_body"
        => r#"macro_rules! harmless_name { () => { std::env::args().count(); }; } fn f() { harmless_name!(); }"#,
           "`macro_rules!` definitions are not allowed";
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
    for module in REJECTED_STD_MODULES {
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
    // CRITICAL (Codex follow-up review): visit_macro is now a fail-closed
    // ALLOWLIST, not a blocklist -- the mirror check compares against
    // ALLOWED_MACROS instead of FORBIDDEN_MACROS. FORBIDDEN_MACROS (and
    // its per-site tests above) stays a valid, separate assertion that
    // those specific macros are indeed still rejected -- just via the
    // allowlist mechanism rather than an explicit name match.
    let mut expected_macros: Vec<String> =
        ALLOWED_MACROS.iter().map(|s| s.to_string()).collect();
    expected_macros.sort();
    let actual_macros = ident_literals_between("fn visit_macro", "syn::visit::visit_macro");
    assert_eq!(
        actual_macros, expected_macros,
        "ALLOWED_MACROS is out of sync with validator.rs's visit_macro. \
         Update the table if the allowlist itself changed."
    );
    for forbidden in FORBIDDEN_MACROS {
        assert!(
            !ALLOWED_MACROS.contains(forbidden),
            "FORBIDDEN_MACROS and ALLOWED_MACROS must never overlap: {forbidden:?} is in both"
        );
    }

    let permitted = permitted_std_path_literals();
    assert!(!permitted.is_empty(), "no PERMITTED_STD_PATHS literals found in validator/std_paths.rs");
    for module in REJECTED_STD_MODULES {
        let path = format!("std::{module}");
        assert!(
            !permitted.iter().any(|p| path == *p || path.starts_with(&format!("{p}::"))),
            "REJECTED_STD_MODULES and PERMITTED_STD_PATHS must never overlap: {path}"
        );
    }
}

const STD_PATHS_SOURCE: &str = include_str!("../src/validator/std_paths.rs");

/// String literals of the `PERMITTED_STD_PATHS` constant, in source order.
fn permitted_std_path_literals() -> Vec<String> {
    let start = STD_PATHS_SOURCE
        .find("const PERMITTED_STD_PATHS")
        .expect("std_paths.rs no longer defines PERMITTED_STD_PATHS");
    let rest = &STD_PATHS_SOURCE[start..];
    let end = rest.find("];").expect("PERMITTED_STD_PATHS has no closing `];`");
    rest[..end].split('"').skip(1).step_by(2).map(|lit| lit.to_string()).collect()
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
        !errors.iter().any(|e| e.message.starts_with("`use std::collections")),
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
