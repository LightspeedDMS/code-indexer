//! Regression tests for Bug #1921 -- filed against the concern that
//! `apply_private_visibility_filter` (`bind::narrowing`, D2) might
//! wrongly exclude a Kotlin **file-private top-level function** called
//! from a class declared in the SAME file. That call is LEGAL Kotlin
//! (`private` on a top-level declaration means "visible anywhere in this
//! file", not "visible only within its own type" -- there is no
//! enclosing type at all), so if the filter treated it like a
//! private *member* it would produce exactly the false
//! `is_definitely_dead_code() == Some(true)` verdict epic #1786/#1906
//! exist to eliminate.
//!
//! **What this investigation actually found, verified end-to-end via
//! `build_repo_graph` (not merely reasoned about):**
//!
//! `apply_private_visibility_filter` only excludes a candidate when its
//! `DeclInfo::enclosing_type` is `Some` AND differs from the caller's own
//! top-level type (`rust/xray-core/src/graph/bind/narrowing.rs:567`).
//! `DeclInfo::enclosing_type` is populated exclusively from
//! `LocalIndex::method_owners` (`bind/name_index.rs:78`,
//! `owners_by_symbol.get(&decl.symbol)`), and the Kotlin extractor
//! (`extract_function_declaration` in `kotlin.rs`) pushes a
//! `MethodOwnerRecord` ONLY `if let Some(enclosing_type) = enclosing_type`
//! -- for a genuine TOP-LEVEL function (no surrounding `class`/`object`/
//! `interface`), `ctx.enclosing_type` is `None` all the way from the
//! file's root `WalkContext`, so no owner record is ever pushed. The
//! resulting `DeclInfo::enclosing_type` is `None`, which hits the
//! filter's own early-return guard (`let Some(owner) = decl.enclosing_
//! type.as_deref() else { return true; }`) -- the filter never even
//! evaluates visibility for a top-level declaration. It simply does not
//! apply to this shape, in EITHER direction (see the cross-file test
//! below), by construction, on the current tree, with zero changes to
//! `narrowing.rs`.
//!
//! `#[test] kotlin_file_private_top_level_function_called_from_class_in_same_file_is_not_reported_dead`
//! is the DISCRIMINATING proof for the shape #1921 actually raised
//! (legal same-file call): it fails loudly if a future change to
//! `apply_private_visibility_filter` starts synthesizing an owner for a
//! top-level declaration without also carving out the same-file
//! exemption Kotlin's real visibility rule requires.
//!
//! The mirror (illegal cross-file) and #1921's own literal repro (a
//! private MEMBER, not top-level, called from an unrelated class) are
//! pinned too, for contrast. Both of those two fixtures are deliberately
//! NON-compiling Kotlin/Java (that illegality is the whole point -- they
//! exist to prove the binder's response to code that could never really
//! exist is on the SAFE side, not to claim it is valid source). Neither
//! is the bug #1921 hypothesized, and both already behave per the epic's
//! own "over-binding is safe, under-binding is not" doctrine
//! (`docs/xray-architecture.md`'s candidate-admission section).
//!
//! The SAME_FILE fixture above, by contrast, IS real, compilable Kotlin.

use std::path::Path;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::local_index::{Declaration, LocalIndex};
use xray_core::graph::identity::SymbolId;
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;

struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

fn write_source(dir: &Path, relative_path: &str, source: &str) {
    let full = dir.join(relative_path);
    if let Some(parent) = full.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(full, source).unwrap();
}

fn extract_index(dir: &Path, relative_path: &str) -> LocalIndex {
    let full_path = dir.join(relative_path);
    let result = xray_core::graph::fused::process_file_fused(&full_path, relative_path, &NoOpCollector)
        .unwrap_or_else(|| panic!("fixture bug: {relative_path} must parse"));
    result
        .index
        .unwrap_or_else(|| panic!("fixture bug: {relative_path} must extract (language must be supported)"))
}

/// Looks up a TOP-LEVEL declaration by bare name (no owning type --
/// `method_owners` carries no record for it at all). Panics if the name
/// is absent or ambiguous, mirroring `bug_1908_kotlin_graph_extractor.
/// rs`'s identical helper.
fn declaration_symbol(index: &LocalIndex, name: &str) -> SymbolId {
    let matches: Vec<&Declaration> = index.declarations.iter().filter(|d| d.name == name).collect();
    match matches.as_slice() {
        [only] => only.symbol,
        [] => panic!("fixture bug: no declaration named {name:?}"),
        other => panic!("fixture bug: {name:?} is ambiguous ({} declarations)", other.len()),
    }
}

fn declaration_symbol_owned_by(index: &LocalIndex, name: &str, enclosing_type: &str) -> SymbolId {
    let owner_symbols: std::collections::HashSet<SymbolId> = index
        .method_owners
        .iter()
        .filter(|o| o.enclosing_type == enclosing_type)
        .map(|o| o.method_symbol)
        .collect();
    index
        .declarations
        .iter()
        .find(|d| d.name == name && owner_symbols.contains(&d.symbol))
        .unwrap_or_else(|| panic!("fixture bug: no {name:?} declaration owned by {enclosing_type:?}"))
        .symbol
}

fn build_graph_over(dir: &Path, relative_paths: &[&str]) -> CodeGraph {
    let options = RepoIndexOptions {
        budget: IndexBudget::unlimited(),
        max_files: None,
    };
    let paths: Vec<String> = relative_paths.iter().map(|p| p.to_string()).collect();
    let result = build_repo_graph(dir, &paths, &options, &NoOpCollector)
        .expect("no file_id collision in this fixture");
    result.graph
}

fn dead_and_caller_count(graph: &CodeGraph, symbol: SymbolId) -> (Option<bool>, usize) {
    let dense = graph
        .dense_id_for(symbol)
        .expect("symbol must be interned in the bound graph");
    (graph.is_definitely_dead_code(dense), graph.callers_index(dense).len())
}

const SAME_FILE_PRIVATE_TOP_LEVEL: &str = r#"package com.example.app

private fun fileHelper(raw: String): String = raw.trim()

class Caller {
    fun use(raw: String): String = fileHelper(raw)
}
"#;

/// The shape #1921 actually hypothesized: `private` top-level Kotlin
/// function, called from a class declared in the SAME FILE. This IS
/// legal, compilable Kotlin -- file-private, not type-private. Must never
/// be reported definitely dead.
#[test]
fn kotlin_file_private_top_level_function_called_from_class_in_same_file_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Helper.kt", SAME_FILE_PRIVATE_TOP_LEVEL);

    let index = extract_index(dir.path(), "com/example/app/Helper.kt");
    let helper_symbol = declaration_symbol(&index, "fileHelper");

    let graph = build_graph_over(dir.path(), &["com/example/app/Helper.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, helper_symbol);
    assert_eq!(
        dead,
        Some(false),
        "a file-private top-level Kotlin function called from a class in the SAME file is legal \
         Kotlin and must never be reported definitely dead"
    );
    assert!(callers >= 1, "fileHelper must keep its real same-file caller edge");
}

const CROSS_FILE_PRIVATE_TOP_LEVEL_DECL: &str = r#"package com.example.app

private fun fileHelper(raw: String): String = raw.trim()
"#;
const CROSS_FILE_PRIVATE_TOP_LEVEL_CALLER: &str = r#"package com.example.app

class OtherCaller {
    fun use(raw: String): String = fileHelper(raw)
}
"#;

/// Mirror (documented, not a defect). `CROSS_FILE_PRIVATE_TOP_LEVEL_
/// CALLER` is DELIBERATELY non-compiling Kotlin -- calling the SAME
/// file-private top-level function from a class in a DIFFERENT file is
/// illegal (would not compile), included specifically to probe the
/// binder's response to that illegal shape. The graph still keeps it
/// alive here, because `apply_private_visibility_filter` never evaluates
/// a top-level declaration at all (no `enclosing_type` record exists to
/// compare). This is over-binding, the epic's own accepted-safe direction
/// (never under-binds a real caller); it is explicitly NOT the
/// false-dead-verdict class #1786/#1906 guard against, so this test pins
/// current behavior for visibility rather than demanding a fix.
#[test]
fn kotlin_top_level_private_called_from_a_different_file_over_binds_not_under_binds() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/HelperOnly.kt",
        CROSS_FILE_PRIVATE_TOP_LEVEL_DECL,
    );
    write_source(
        dir.path(),
        "com/example/app/OtherCaller.kt",
        CROSS_FILE_PRIVATE_TOP_LEVEL_CALLER,
    );

    let index = extract_index(dir.path(), "com/example/app/HelperOnly.kt");
    let helper_symbol = declaration_symbol(&index, "fileHelper");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/HelperOnly.kt", "com/example/app/OtherCaller.kt"],
    );
    let (dead, _callers) = dead_and_caller_count(&graph, helper_symbol);
    assert_eq!(
        dead,
        Some(false),
        "top-level declarations carry no owner record at all, so the D2 visibility filter never \
         applies to them regardless of caller file -- documented over-binding, the epic's accepted \
         safe direction, not the under-binding defect #1921 was filed to describe"
    );
}

const JAVA_UTIL_PRIVATE_MEMBER: &str = r#"package com.example.app;

public class JavaUtil {
    private static String helper(String raw) {
        return raw.trim();
    }
}
"#;
const KOTLIN_CALLER_DIFFERENT_TOP_LEVEL_TYPE: &str = r#"package com.example.app

class Caller {
    fun useJavaHelper(g: JavaUtil, raw: String): String {
        return g.helper(raw)
    }
}
"#;

/// #1921's own literal repro. `KOTLIN_CALLER_DIFFERENT_TOP_LEVEL_TYPE` is
/// DELIBERATELY non-compiling Kotlin/Java: a `private` MEMBER method (not
/// top-level), called from a class declared inside a DIFFERENT top-level
/// type -- hand-verified via `javac` to fail with `helper(String) has
/// private access in JavaUtil`. Included specifically to probe the
/// binder's response to that illegal shape: `is_definitely_dead_code() ==
/// Some(true)` here is the CORRECT verdict, not a false one, since no
/// legal caller can exist. Pinned so a future change cannot "fix" this
/// non-bug into an actual under-binding defect.
#[test]
fn private_member_called_from_a_different_top_level_type_correctly_stays_dead_matching_javac() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/JavaUtil.java", JAVA_UTIL_PRIVATE_MEMBER);
    write_source(
        dir.path(),
        "com/example/app/KotlinCaller.kt",
        KOTLIN_CALLER_DIFFERENT_TOP_LEVEL_TYPE,
    );

    let java_index = extract_index(dir.path(), "com/example/app/JavaUtil.java");
    let helper_symbol = declaration_symbol_owned_by(&java_index, "helper", "JavaUtil");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/JavaUtil.java", "com/example/app/KotlinCaller.kt"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, helper_symbol);
    assert_eq!(
        dead,
        Some(true),
        "a private member called from an unrelated top-level type does not compile in real Java/ \
         Kotlin -- correctly reported dead, matching javac, not a false-dead-verdict defect"
    );
    assert_eq!(callers, 0, "no legal caller exists for this shape -- zero callers is correct");
}
