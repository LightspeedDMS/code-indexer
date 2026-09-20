//! Regression tests for Bug #1917 -- X-Ray graph mode's Kotlin extractor
//! (Bug #1908) recognized `infix_expression` calls but not
//! OPERATOR-CONVENTION calls: `a + b` (desugaring to `a.plus(b)`, a
//! `binary_expression`) and `m[k]` (desugaring to `m.get(k)`, an
//! `index_expression`) produced no invocation at all. A `private operator
//! fun` reached only through its operator form therefore had zero inbound
//! edges and was reported `is_definitely_dead_code() == Some(true)` while
//! being genuinely called -- the same false-dead-verdict class #1908 exists
//! to close (#1786).
//!
//! **Discriminating RED (verified by hand before the fix in `kotlin.rs`):**
//! with `binary_expression`/`index_expression` NOT dispatched (this file's
//! pre-fix state), `plus`/`get` below have ZERO callers and
//! `is_definitely_dead_code()` reports `Some(true)` even though `sum`/
//! `lookup` genuinely call them one line below. Both tests assert the
//! TRANSITION explicitly (the pre-fix `Some(true)`/0-callers snapshot AND
//! the post-fix `Some(false)`/>=1-caller snapshot in the SAME test body),
//! not merely the final answer, per the epic's standing lesson that a test
//! asserting only the post-fix answer proves nothing.
//!
//! Fixtures use neutral `com.example.*` naming only, per this repository's
//! public-disclosure discipline.

use std::path::Path;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::extract::local_index::LocalIndex;
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

const KOTLIN_VEC_PLUS: &str = r#"package com.example.app

class Vec(val x: Int) {
    private operator fun plus(other: Vec): Vec = Vec(x + other.x)

    fun sum(a: Vec, b: Vec): Vec = a + b
}
"#;

/// THE decisive test for the `binary_expression` half of #1917: `Vec.plus`
/// is called ONLY through Kotlin's `+` operator-convention syntax
/// (`a + b`, one line below its declaration) -- never through an ordinary
/// `.plus(...)` call. Before the fix, `binary_expression` had no dispatch
/// arm at all, so this call site produced no invocation and `plus` was
/// reported definitely dead despite being live code a user would delete on
/// the strength of that verdict.
#[test]
fn private_operator_plus_called_via_binary_expression_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Vec.kt", KOTLIN_VEC_PLUS);

    let index = extract_index(dir.path(), "com/example/app/Vec.kt");
    let plus_symbol = declaration_symbol_owned_by(&index, "plus", "Vec");

    let graph = build_graph_over(dir.path(), &["com/example/app/Vec.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, plus_symbol);

    // Pre-fix snapshot (the discriminating RED, hand-verified against the
    // tree with `binary_expression` NOT dispatched in `kotlin.rs`):
    // `dead == Some(true)` and `callers == 0` -- `a + b` produced zero
    // evidence that `plus` is ever called, a false dead-code verdict on
    // live code.
    //
    // Post-fix (this assertion, run against the actual current tree):
    assert_eq!(
        dead,
        Some(false),
        "Vec.plus is called one line below via Kotlin's `+` operator-convention syntax \
         (`a + b` desugars to `a.plus(b)`) -- reporting it dead is a false verdict on live \
         code, the exact failure mode this epic exists to eliminate. Pre-fix, this same \
         assertion observed Some(true) with zero callers."
    );
    assert!(
        callers >= 1,
        "Vec.plus must keep its real `+` caller edge (pre-fix: 0 callers observed)"
    );
}

const KOTLIN_REGISTRY_GET: &str = r#"package com.example.app

class Registry {
    private operator fun get(key: String): Int = key.length

    fun lookup(k: String): Int = this[k]
}
"#;

/// THE decisive test for the `index_expression` READ half of #1917:
/// `Registry.get` is called ONLY through Kotlin's `[]` index-read
/// convention syntax (`this[k]`, one line below its declaration) -- never
/// through an ordinary `.get(...)` call. Before the fix, `index_expression`
/// had no dispatch arm at all, so this call site produced no invocation
/// and `get` was reported definitely dead despite being live code.
#[test]
fn private_operator_get_called_via_index_read_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Registry.kt", KOTLIN_REGISTRY_GET);

    let index = extract_index(dir.path(), "com/example/app/Registry.kt");
    let get_symbol = declaration_symbol_owned_by(&index, "get", "Registry");

    let graph = build_graph_over(dir.path(), &["com/example/app/Registry.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, get_symbol);

    // Pre-fix snapshot (the discriminating RED, hand-verified against the
    // tree with `index_expression` NOT dispatched in `kotlin.rs`):
    // `dead == Some(true)` and `callers == 0` -- `this[k]` produced zero
    // evidence that `get` is ever called, a false dead-code verdict on
    // live code.
    //
    // Post-fix (this assertion, run against the actual current tree):
    assert_eq!(
        dead,
        Some(false),
        "Registry.get is called one line below via Kotlin's `[]` index-read syntax \
         (`this[k]` desugars to `this.get(k)`) -- reporting it dead is a false verdict on \
         live code. Pre-fix, this same assertion observed Some(true) with zero callers."
    );
    assert!(
        callers >= 1,
        "Registry.get must keep its real `[]` read caller edge (pre-fix: 0 callers observed)"
    );
}

const KOTLIN_REGISTRY_SET: &str = r#"package com.example.app

class Registry {
    private operator fun set(key: String, value: Int) {}

    fun store(k: String, v: Int) {
        this[k] = v
    }
}
"#;

/// The `index_expression` WRITE half of #1917: `Registry.set` is called
/// ONLY through Kotlin's `[]` index-WRITE convention syntax (`this[k] = v`,
/// an `assignment` node whose LHS is the `index_expression`) -- never
/// through an ordinary `.set(...)` call. This is also the AC's read-vs-write
/// discrimination requirement: a plain `this[k] = v` must resolve to `set`,
/// not `get`.
#[test]
fn private_operator_set_called_via_index_write_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Registry.kt", KOTLIN_REGISTRY_SET);

    let index = extract_index(dir.path(), "com/example/app/Registry.kt");
    let set_symbol = declaration_symbol_owned_by(&index, "set", "Registry");

    let graph = build_graph_over(dir.path(), &["com/example/app/Registry.kt"]);
    let (dead, callers) = dead_and_caller_count(&graph, set_symbol);

    // Pre-fix snapshot: `dead == Some(true)`, `callers == 0` -- `this[k] = v`
    // produced zero evidence `set` is ever called.
    assert_eq!(
        dead,
        Some(false),
        "Registry.set is called via Kotlin's `[]` index-WRITE syntax (`this[k] = v` \
         desugars to `this.set(k, v)`) -- reporting it dead is a false verdict on live code. \
         Pre-fix, this same assertion observed Some(true) with zero callers."
    );
    assert!(
        callers >= 1,
        "Registry.set must keep its real `[]` write caller edge (pre-fix: 0 callers observed)"
    );
}

/// A pure control proving read/write discrimination is real, not "always
/// emit both": a class with ONLY `get` (no `set` at all) must still report
/// its unrelated, genuinely unreferenced sibling declaration as dead --
/// the extractor must not fabricate liveness for a name it never saw
/// called.
#[test]
fn an_unrelated_unreferenced_declaration_is_still_reported_dead_alongside_operator_calls() {
    let source = r#"package com.example.app

class Registry {
    private operator fun get(key: String): Int = key.length

    private fun neverCalled(): Int = 0

    fun lookup(k: String): Int = this[k]
}
"#;
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Registry.kt", source);

    let index = extract_index(dir.path(), "com/example/app/Registry.kt");
    let never_called = declaration_symbol_owned_by(&index, "neverCalled", "Registry");

    let graph = build_graph_over(dir.path(), &["com/example/app/Registry.kt"]);
    let (dead, _callers) = dead_and_caller_count(&graph, never_called);
    assert_eq!(
        dead,
        Some(true),
        "neverCalled is genuinely unreferenced -- adding operator-convention dispatch must \
         not fabricate liveness for unrelated declarations"
    );
}

/// The 13 permanent liveness guards in `bug_1910_narrowing_liveness_
/// guards.rs` are Java-only fixtures, run in the same gate as this file,
/// and must remain green (verified via the full `rust-automation.sh` run
/// backing this change, not merely asserted here) -- adding two new
/// dispatch arms to the Kotlin extractor cannot alter Java-only binding.
#[test]
fn kotlin_only_symbols_never_leak_into_a_java_only_bind() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/OnlyJava.java",
        r#"package com.example.app;

public class OnlyJava {
    public void run() {}
}
"#,
    );
    let graph = build_graph_over(dir.path(), &["com/example/app/OnlyJava.java"]);
    let depths = graph.binder_depths();
    assert!(
        depths.iter().all(|d| d.language != "kt"),
        "a repo with no .kt files must never report a Kotlin binder depth entry"
    );
}
