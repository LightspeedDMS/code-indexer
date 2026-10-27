use super::validate_evaluator_source;

fn assert_rejected_naming(source: &str, fragment: &str) {
    let errors = validate_evaluator_source(source)
        .expect_err(&format!("source must be rejected:\n{source}"));
    assert!(
        errors.iter().any(|e| e.message.contains(fragment)),
        "expected an error naming {fragment:?} for:\n{source}\ngot: {:?}",
        errors.iter().map(|e| &e.message).collect::<Vec<_>>()
    );
}

fn assert_accepted(source: &str) {
    assert!(
        validate_evaluator_source(source).is_ok(),
        "source must be accepted:\n{source}\ngot: {:?}",
        validate_evaluator_source(source).err()
    );
}

// ---- Python pre-flight mirror ----

const PYTHON_MIRROR: &str = include_str!("../../../../src/code_indexer/xray/rust_std_paths.py");

#[test]
fn python_mirror_matches_permitted_std_paths() {
    let start = PYTHON_MIRROR
        .find("PERMITTED_STD_PATHS: Tuple[str, ...] = (")
        .expect("rust_std_paths.py no longer defines PERMITTED_STD_PATHS");
    let rest = &PYTHON_MIRROR[start..];
    let end = rest.find(')').expect("PERMITTED_STD_PATHS tuple is not closed");
    let mirrored: Vec<&str> = rest[..end].split('"').skip(1).step_by(2).collect();
    assert_eq!(mirrored, super::std_paths::PERMITTED_STD_PATHS.to_vec());
}

// ---- shipped seed evaluators ----

const SEED_PATTERNS: &str = include_str!("../../../../src/code_indexer/server/services/xray_pattern_seeds.py");
const EXPECTED_SEED_EVALUATORS: usize = 2;

fn seed_evaluator_sources() -> Vec<String> {
    let mut sources = Vec::new();
    let mut current: Option<Vec<&str>> = None;
    for line in SEED_PATTERNS.lines() {
        match current.take() {
            None if line.starts_with("evaluator_code: |") => current = Some(Vec::new()),
            None => {}
            Some(mut body) => match line.strip_prefix("  ") {
                Some(code) => {
                    body.push(code);
                    current = Some(body);
                }
                None if line.is_empty() => {
                    body.push("");
                    current = Some(body);
                }
                None => sources.push(body.join("\n")),
            },
        }
    }
    if let Some(body) = current {
        sources.push(body.join("\n"));
    }
    sources
}

#[test]
fn accepts_shipped_seed_evaluators() {
    let sources = seed_evaluator_sources();
    assert_eq!(sources.len(), EXPECTED_SEED_EVALUATORS, "seed evaluator_code blocks found: {sources:?}");
    for source in sources {
        assert_accepted(&source);
    }
}

// ---- documented evaluator examples ----

const DOCUMENTED_EXAMPLES: &[(&str, &str)] = &[
    ("xray_search.md", include_str!("../../../../src/code_indexer/server/mcp/tool_docs/search/xray_search.md")),
    ("xray_explore.md", include_str!("../../../../src/code_indexer/server/mcp/tool_docs/search/xray_explore.md")),
    ("analyze_graph.md", include_str!("../../../../src/code_indexer/server/mcp/tool_docs/search/analyze_graph.md")),
    ("xray-cookbook.md", include_str!("../../../../docs/guides/xray-cookbook.md")),
    ("ADR-001", include_str!("../../../../docs/adr/ADR-001-xray-evaluator-execution-modes.md")),
];
const MIN_DOCUMENTED_EXAMPLES: usize = 10;

fn rust_blocks_with_entry_point(markdown: &str) -> Vec<String> {
    let mut blocks = Vec::new();
    let mut current: Option<Vec<&str>> = None;
    for line in markdown.lines() {
        let fence = line.trim_start();
        match current.take() {
            None if fence == "```rust" => current = Some(Vec::new()),
            None => {}
            Some(body) if fence.starts_with("```") => blocks.push(body.join("\n")),
            Some(mut body) => {
                body.push(line);
                current = Some(body);
            }
        }
    }
    blocks
        .into_iter()
        .filter(|b| b.contains("fn evaluate_node") || b.contains("fn collect_facts") || b.contains("fn refine"))
        .collect()
}

// ^ invariant (doc, first trimmed line) of every documented block that names an entry point but is not a full evaluator
const SIGNATURE_ONLY_BLOCKS: &[(&str, &str)] = &[
    ("xray_search.md", "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>"),
    ("xray_explore.md", "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>"),
    ("ADR-001", "fn collect_facts(node: &OwnedNode, file: &FileContext, index: &LocalIndex)"),
];

fn first_line(block: &str) -> &str {
    block.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or("")
}

#[test]
fn accepts_documented_evaluator_examples() {
    let mut checked = 0;
    let mut signature_only_seen: Vec<(String, String)> = Vec::new();
    for (doc, markdown) in DOCUMENTED_EXAMPLES {
        for block in rust_blocks_with_entry_point(markdown) {
            if syn::parse_file(&block).is_err() {
                let key = (*doc, first_line(&block));
                assert!(
                    SIGNATURE_ONLY_BLOCKS.contains(&key),
                    "documented block in {doc} names an entry point but does not parse; fix it or list {key:?}:\n{block}"
                );
                signature_only_seen.push((key.0.to_string(), key.1.to_string()));
                continue;
            }
            let result = validate_evaluator_source(&block);
            assert!(result.is_ok(), "documented example in {doc} must be accepted:\n{block}\ngot: {:?}", result.err());
            checked += 1;
        }
    }
    for listed in SIGNATURE_ONLY_BLOCKS {
        assert!(
            signature_only_seen.iter().any(|(d, l)| d == listed.0 && l == listed.1),
            "stale SIGNATURE_ONLY_BLOCKS entry: {listed:?}"
        );
    }
    assert!(checked >= MIN_DOCUMENTED_EXAMPLES, "documented evaluator examples checked: {checked}");
}

// ---- path expressions ----

#[test]
fn rejects_std_os_path() {
    assert_rejected_naming("fn f() { let _ = std::os::unix::ffi::OsStrExt::as_bytes; }", "std::os");
}

#[test]
fn rejects_std_thread_path() {
    assert_rejected_naming("fn f() { std::thread::yield_now(); }", "std::thread");
}

#[test]
fn rejects_std_ffi_path() {
    assert_rejected_naming("fn f() { let _ = std::ffi::OsString::new(); }", "std::ffi");
}

#[test]
fn rejects_std_path_module_path() {
    assert_rejected_naming(r#"fn f() { let _ = std::path::Path::new("x"); }"#, "std::path");
}

#[test]
fn rejects_std_path_in_type_position() {
    assert_rejected_naming("fn f(_t: std::thread::ThreadId) {}", "std::thread");
}

#[test]
fn rejects_leading_colon_std_path() {
    assert_rejected_naming("fn f() { ::std::thread::yield_now(); }", "std::thread");
}

#[test]
fn rejects_raw_identifier_std_root() {
    assert_rejected_naming("fn f() { r#std::thread::yield_now(); }", "std::thread");
}

#[test]
fn rejects_raw_identifier_std_submodule() {
    assert_rejected_naming("fn f() { std::r#process::exit(0); }", "std::process");
}

#[test]
fn rejects_core_path_outside_allowlist() {
    assert_rejected_naming("fn f() { let _ = core::mem::size_of::<u8>(); }", "core::mem");
}

#[test]
fn rejects_alloc_path_outside_allowlist() {
    assert_rejected_naming("fn f() { let _ = alloc::alloc::Layout::new::<u8>(); }", "alloc::alloc");
}

#[test]
fn rejects_original_five_modules_as_path_expressions() {
    for module in ["fs", "net", "process", "env", "io"] {
        let source = format!("fn f() {{ let _ = std::{module}::x; }}");
        assert_rejected_naming(&source, &format!("std::{module}"));
    }
}

#[test]
fn rejects_std_path_inside_allowlisted_macro_tokens() {
    assert_rejected_naming(r#"fn f() { let _ = format!("{:?}", std::thread::current()); }"#, "std::thread");
}

// ---- extern crate ----

#[test]
fn rejects_extern_crate() {
    assert_rejected_naming("extern crate std as s;\nfn f() { s::thread::yield_now(); }", "`extern crate`");
}

#[test]
fn rejects_extern_crate_without_rename() {
    assert_rejected_naming("extern crate alloc;", "`extern crate`");
}

#[test]
fn rejects_extern_crate_self() {
    assert_rejected_naming("extern crate self as me;", "`extern crate`");
}

// ---- module-level allowlist: independent of PERMITTED_STD_PATHS on purpose ----

const ALLOWED_STD_MODULES: &[&str] = &[
    "collections", "fmt", "iter", "cmp", "str", "string", "vec", "option", "result", "ops", "convert", "char",
    "num", "hash", "borrow", "default", "boxed", "rc", "cell", "array", "slice", "marker", "primitive", "f32",
    "f64", "i8", "i16", "i32", "i64", "i128", "u8", "u16", "u32", "u64", "u128", "isize", "usize", "error", "any",
];
const STD_ONLY_MODULES: &[&str] = &["collections", "string", "vec", "boxed", "rc"];
const ALLOWED_ALLOC_MODULES: &[&str] = &["vec", "string", "collections", "boxed", "rc"];
const REJECTED_MODULES: &[&str] = &[
    "fs", "net", "process", "env", "io", "os", "thread", "sync", "time", "ffi", "path", "ptr", "mem", "alloc",
    "panic", "arch", "intrinsics", "simd", "hint", "backtrace",
];

fn module_forms(root: &str, module: &str) -> [String; 3] {
    [
        format!("use {root}::{module};"),
        format!("use {root}::{module}::Item;"),
        format!("fn f() {{ let _ = {root}::{module}::item; }}"),
    ]
}

#[test]
fn accepts_allowlisted_modules_in_use_and_path_form() {
    let core_modules: Vec<&str> = ALLOWED_STD_MODULES.iter().copied().filter(|m| !STD_ONLY_MODULES.contains(m)).collect();
    for (root, modules) in [("std", ALLOWED_STD_MODULES.to_vec()), ("core", core_modules), ("alloc", ALLOWED_ALLOC_MODULES.to_vec())] {
        for module in modules {
            for source in module_forms(root, module) {
                assert_accepted(&source);
            }
        }
    }
}

#[test]
fn rejects_modules_outside_the_allowlist_under_every_root() {
    for root in ["std", "core", "alloc"] {
        for module in REJECTED_MODULES {
            for source in module_forms(root, module) {
                assert_rejected_naming(&source, &format!("{root}::{module}"));
            }
        }
    }
}

#[test]
fn rejects_modules_absent_from_the_root_allowlist() {
    for module in STD_ONLY_MODULES {
        for source in module_forms("core", module) {
            assert_rejected_naming(&source, &format!("core::{module}"));
        }
    }
    for module in ["fmt", "str", "slice", "borrow", "any", "error"] {
        for source in module_forms("alloc", module) {
            assert_rejected_naming(&source, &format!("alloc::{module}"));
        }
    }
}

// ---- use trees ----

#[test]
fn rejects_renamed_std_root() {
    assert_rejected_naming("use std as s;\nfn f() { s::thread::yield_now(); }", "renames a standard-library path");
}

#[test]
fn rejects_renamed_std_root_in_self_group() {
    assert_rejected_naming("use std::{self as s};\nfn f() { s::thread::yield_now(); }", "renames a standard-library path");
}

#[test]
fn rejects_renamed_std_root_in_top_level_group() {
    assert_rejected_naming("use {std as s};", "renames a standard-library path");
}

#[test]
fn rejects_renamed_core_and_alloc_roots() {
    assert_rejected_naming("use core as c;", "renames a standard-library path");
    assert_rejected_naming("use alloc as a;", "renames a standard-library path");
}

#[test]
fn rejects_renamed_std_module() {
    assert_rejected_naming("use std::collections as c;", "renames a standard-library path");
}

#[test]
fn rejects_any_rename_in_std_import() {
    assert_rejected_naming("use std::collections::HashMap as Map;", "renames a standard-library path");
}

#[test]
fn rejects_std_root_import() {
    assert_rejected_naming("use std;", "`use std`");
}

#[test]
fn rejects_std_os_thread_ffi_path_imports() {
    for module in ["os", "thread", "ffi", "path"] {
        assert_rejected_naming(&format!("use std::{module};"), &format!("`use std::{module}`"));
        assert_rejected_naming(&format!("use std::{module}::Item;"), &format!("`use std::{module}::Item`"));
    }
}

#[test]
fn rejects_original_five_modules_as_imports() {
    for module in ["fs", "net", "process", "env", "io"] {
        assert_rejected_naming(&format!("use std::{module}::Item;"), &format!("`use std::{module}::Item`"));
        assert_rejected_naming(&format!("use std::{module};"), &format!("`use std::{module}`"));
    }
}

#[test]
fn rejects_disallowed_member_of_std_group() {
    assert_rejected_naming("use std::{collections::HashMap, thread};", "`use std::thread`");
}

#[test]
fn rejects_raw_identifier_std_import() {
    assert_rejected_naming("use r#std::r#thread;", "`use std::thread`");
}

#[test]
fn rejects_glob_import_outside_allowlist() {
    assert_rejected_naming("use std::*;", "(glob import)");
    assert_rejected_naming("use std::os::*;", "(glob import)");
}

#[test]
fn rejects_core_import_outside_allowlist() {
    assert_rejected_naming("use core::mem;", "`use core::mem`");
}

#[test]
fn accepts_allowlisted_imports() {
    assert_accepted(
        r#"
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::collections::hash_map::Entry;
use std::fmt::{self, Write};
use std::iter;
use ::std::collections::VecDeque;
use std::{collections::LinkedList, fmt::Display};
use std::collections::*;
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    *counts.entry(node.kind.clone()).or_insert(0) += 1;
    let _ = iter::once(1);
    Vec::new()
}
"#,
    );
}

#[test]
fn accepts_renames_outside_std() {
    assert_accepted("use crate::OwnedNode as Node;\nfn f(_n: &Node) {}");
}

#[test]
fn accepts_allowlisted_path_expressions() {
    assert_accepted(
        r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut seen: std::collections::HashSet<String> = std::collections::HashSet::new();
    let _ = seen.insert(node.kind.clone());
    let filler: String = std::iter::repeat('b').take(3).collect();
    let _w: Option<std::fmt::Error> = None;
    let _ = filler;
    Vec::new()
}
"#,
    );
}
