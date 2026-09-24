//! Regression test for Bug #1954 -- the project's own Java+Kotlin
//! "Quick Start" demonstration fixture used a `private` Java method as its
//! cross-language target, so the Kotlin call to it was not legal Java/
//! Kotlin in the first place. Running the documented Quick Start dead-code
//! template (`docs/xray-cookbook.md` / `analyze_graph.md`, "Java and
//! Kotlin bind to EACH OTHER") against that illegal fixture reported the
//! showcase method DEAD, with `fact_graph_complete: true` and every
//! degradation counter at zero -- the FIXTURE was wrong, not the binder:
//! `apply_private_visibility_filter` (D2, `bind::narrowing`) correctly
//! discards a private candidate declared in a different known top-level
//! type (`docs/xray-architecture.md`'s own documented rule), and a Java
//! `private` instance method called via an instance-qualified receiver
//! from a DIFFERENT top-level Kotlin type in a DIFFERENT file is exactly
//! that shape -- it is not legal Java/Kotlin as written, so the exclusion
//! fires correctly and the false-dead-looking verdict follows.
//!
//! This is a DIFFERENT fixture shape from `bug_1908_kotlin_graph_
//! extractor.rs`'s own `java_method_called_only_from_kotlin_is_not_
//! reported_dead` (a `private static` method reached through a
//! TYPE-qualified call, `JavaUtil.helper(raw)`), which resolves through a
//! different bind path and was, before this fix, the repository's only
//! claimed proof that Kotlin->Java binding works at all -- yet an
//! instance-qualified cross-file call to a `private` INSTANCE method
//! (`g.helperUsedOnlyFromKotlin(name)`, the shape an arms-length evaluator
//! actually hit first) was never covered and reproduces the false-dead
//! verdict on the current tree (hand-verified before this file was
//! written: with `helperUsedOnlyFromKotlin` still `private`, `dead ==
//! Some(true)` and `callers == 0`; the fix below is renaming it to
//! Java's default package-private access, after which `dead ==
//! Some(false)` and `callers >= 1`).
//!
//! `Greeter`/`KotlinCaller` here IS the project's own Java+Kotlin Quick
//! Start demonstration fixture (also mirrored as the `xray-java-kotlin-
//! fixture-global` golden repo on staging, refreshed separately from this
//! commit) -- fixing it here is what makes the deployed demonstration, and
//! the documented "Java and Kotlin bind to EACH OTHER" claim, actually
//! evidenced rather than merely asserted.

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

fn dead_and_caller_count(graph: &CodeGraph, symbol: SymbolId) -> (Option<bool>, usize) {
    let dense = graph
        .dense_id_for(symbol)
        .expect("symbol must be interned in the bound graph");
    (graph.is_definitely_dead_code(dense), graph.callers_index(dense).len())
}

/// THE fixed Quick Start demonstration fixture (Bug #1954). `helperUsedOnly
/// FromKotlin` is Java's DEFAULT (package-private) access -- both files
/// declare `package com.example.app`, so the Kotlin call is legal Java/
/// Kotlin as written. `privateHelperNotVisibleToKotlin` is a distinct,
/// deliberately PRIVATE sibling, never called from anywhere, kept under a
/// name that cannot be confused with the legal case above -- it stays a
/// live regression guard that the D2 exclusion rule still fires for a
/// genuinely-private, genuinely-unreferenced method after this fix.
const GREETER_JAVA: &str = r#"package com.example.app;

public class Greeter {
    String helperUsedOnlyFromKotlin(String name) {
        return "Hello " + name;
    }

    private String privateHelperNotVisibleToKotlin(String name) {
        return "Hi " + name;
    }
}
"#;

const KOTLIN_CALLER: &str = r#"package com.example.app

class KotlinCaller {
    fun invokeHelper(g: Greeter, name: String): String = g.helperUsedOnlyFromKotlin(name)
}
"#;

/// Shared fixture setup for both tests below: writes the Quick Start
/// `Greeter.java`/`KotlinCaller.kt` pair into a fresh temp dir, extracts
/// `Greeter`'s own `LocalIndex` (for symbol lookups), and binds the full
/// two-file graph -- returned together so each test only has to look up
/// the symbol it cares about.
fn build_fixture_graph() -> (tempfile::TempDir, LocalIndex, CodeGraph) {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Greeter.java", GREETER_JAVA);
    write_source(dir.path(), "com/example/app/KotlinCaller.kt", KOTLIN_CALLER);

    let java_index = extract_index(dir.path(), "com/example/app/Greeter.java");

    let options = RepoIndexOptions {
        budget: IndexBudget::unlimited(),
        max_files: None,
    };
    let paths = vec![
        "com/example/app/Greeter.java".to_string(),
        "com/example/app/KotlinCaller.kt".to_string(),
    ];
    let result = build_repo_graph(dir.path(), &paths, &options, &NoOpCollector)
        .expect("no file_id collision in this fixture");

    (dir, java_index, result.graph)
}

/// THE positive control this repository was missing (issue #1954): a real,
/// legal Kotlin->Java call must produce a real inbound edge and the target
/// must never be reported definitely dead. Reproduces RED with
/// `helperUsedOnlyFromKotlin` still `private` (dead == Some(true), callers
/// == 0, hand-verified before this file was committed); GREEN on the
/// current (fixed) fixture above.
#[test]
fn kotlin_call_to_package_private_java_method_produces_a_real_edge_and_is_not_reported_dead() {
    let (_dir, java_index, graph) = build_fixture_graph();
    let helper_symbol = declaration_symbol_owned_by(&java_index, "helperUsedOnlyFromKotlin", "Greeter");

    let (dead, callers) = dead_and_caller_count(&graph, helper_symbol);
    assert_eq!(
        dead,
        Some(false),
        "Greeter.helperUsedOnlyFromKotlin is called from KotlinCaller.invokeHelper via a legal, \
         package-private cross-file call -- it must never be reported definitely dead. THIS is \
         the positive control proving Kotlin->Java binding actually works, previously missing \
         (issue #1954): the only fixture claiming to prove it was illegal Java/Kotlin as written."
    );
    assert!(
        callers >= 1,
        "Greeter.helperUsedOnlyFromKotlin must keep its real Kotlin caller edge"
    );
}

/// The exclusion-rule sibling this fix must not silently break: a
/// genuinely PRIVATE, genuinely UNREFERENCED Java method (never called
/// from Kotlin, Java, or anywhere) must still be reported definitely dead
/// after `helperUsedOnlyFromKotlin` lost its `private` modifier --
/// distinguishable by name so nobody re-learns the illegal-fixture lesson
/// the hard way.
#[test]
fn unreferenced_private_java_sibling_stays_reported_dead() {
    let (_dir, java_index, graph) = build_fixture_graph();
    let private_symbol =
        declaration_symbol_owned_by(&java_index, "privateHelperNotVisibleToKotlin", "Greeter");

    let (dead, _callers) = dead_and_caller_count(&graph, private_symbol);
    assert_eq!(
        dead,
        Some(true),
        "privateHelperNotVisibleToKotlin is genuinely unreferenced and private -- it must still \
         be reported definitely dead; fixing the legal sibling above must never fabricate \
         liveness for this one"
    );
}
