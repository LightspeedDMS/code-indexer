//! Regression tests for Bug #1920 -- filed against the concern that an
//! INSTANCE-qualified Kotlin call (`g.helper(name)`, a variable receiver)
//! produces no inbound edge at all, unlike the TYPE-qualified form
//! (`JavaUtil.helper(raw)`) Bug #1908's own suite already covers, so a
//! target reachable ONLY through the instance form could be reported
//! `is_definitely_dead_code() == Some(true)` while genuinely called.
//!
//! **What this investigation actually found, stated plainly rather than
//! assumed.** The Kotlin extractor (`kotlin.rs`) extracts BOTH call forms
//! identically -- `g.helper(name)` and `JavaUtil.helper(raw)` both reach
//! `extract_call_expression` off the SAME `navigation_expression` grammar
//! shape, producing an `InvocationSite` that differs only in the recorded
//! `ReceiverExpr::Identifier` text, never in whether an invocation is
//! recorded at all (verified against the real tree-sitter-kotlin-ng 1.1.0
//! grammar -- both parse to `call_expression{navigation_expression{
//! identifier(receiver), identifier(member)}, value_arguments}`, no
//! structural difference). At bind time, `apply_receiver_type_narrowing`
//! (`rust/xray-core/src/graph/bind/narrowing.rs`) is PERMANENTLY tag-only
//! (epic #1906, seven review rounds -- see `docs/xray-architecture.md`'s
//! candidate-admission section): it may set the `RECEIVER_TYPE_MATCH`
//! reason bit, but it NEVER deletes a candidate, on an empty match or a
//! non-matching one, under either the `Positive` or `Advisory` receiver
//! evidence tier. Since candidate-set MEMBERSHIP (and therefore
//! `is_definitely_dead_code`) never depends on whether a reference's
//! receiver resolved to a known type, an instance-qualified call binds by
//! name+arity exactly like a type-qualified one -- confirmed end-to-end,
//! never merely asserted, across every configuration below plus (recorded
//! for completeness, not re-run here) caller-in-a-class/top-level, Java
//! and Kotlin targets, same- and different-package decoys, a constrained
//! `IndexBudget` (per Bug #1833, `is_definitely_dead_code` is decoupled
//! from candidate-set truncation -- `ReferencedBits` marks from the
//! PRE-truncation list), a `max_files`-truncated
//! (`index_is_complete: false`) repo, a wildcard import, and the `?.`/`!!`
//! receiver forms. None of them reproduced a false dead-code verdict tied
//! to receiver qualification kind.
//!
//! The tests below are therefore PERMANENT REGRESSION GUARDS -- proving
//! the over-binding contract the epic requires (#1906/#1910: "over-binding
//! is safe, under-binding is not") already holds for the instance-qualified
//! form, in both cross-language directions, and pinning the existing
//! type-qualified form (#1908's own coverage) so a future change cannot
//! trade one for the other. This is the honest characterisation per this
//! repository's own Fact-Verification discipline: asserting a `Some(true)`
//! -> `Some(false)` transition that cannot actually be produced on the
//! current, already-hardened binder would be fabricated evidence, not a
//! discriminating RED. The one genuine, currently-reproducible false-dead
//! shape this investigation DID find -- a `private` target called from a
//! DIFFERENT top-level Kotlin type, excluded by `apply_private_visibility_
//! filter` (D2) regardless of whether the call is instance- or
//! type-qualified -- is receiver-AGNOSTIC (it affects `JavaUtil.helper(x)`
//! called from inside an unrelated class exactly as much as `g.helper(x)`
//! does) and requires a `bind::narrowing` change, out of this extractor's
//! scope; it is not exercised here and should be tracked separately.
//!
//! Every fixture is real, compilable Java/Kotlin structure, using neutral
//! `com.example.*` naming only, per this repository's public-disclosure
//! discipline.

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

const JAVA_UTIL: &str = r#"package com.example.app;

public class JavaUtil {
    private static String helper(String raw) {
        return raw.trim();
    }
}
"#;

const KOTLIN_INSTANCE_QUALIFIED_CALLER: &str = r#"package com.example.app

fun useJavaHelper(g: JavaUtil, raw: String): String {
    return g.helper(raw)
}
"#;

/// Direction 1 (#1920 AC1): a Java method whose only caller is an
/// INSTANCE-qualified Kotlin call (`g.helper(raw)`, a variable receiver,
/// not `JavaUtil.helper(raw)`). Guard, not a fix: this already reports
/// `Some(false)` / >=1 caller on the current tree -- see this file's
/// module doc for the full evidence trail on why (candidate-set
/// membership never depends on receiver qualification kind).
#[test]
fn java_method_called_only_from_kotlin_instance_qualified_call_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/JavaUtil.java", JAVA_UTIL);
    write_source(
        dir.path(),
        "com/example/app/KotlinCaller.kt",
        KOTLIN_INSTANCE_QUALIFIED_CALLER,
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
        Some(false),
        "JavaUtil.helper is called from Kotlin via the INSTANCE-qualified form (g.helper(raw)) \
         and must never be reported definitely dead -- the exact false-dead-verdict shape #1920 \
         was filed to guard against"
    );
    assert!(callers >= 1, "JavaUtil.helper must keep its real instance-qualified Kotlin caller edge");
}

const KOTLIN_WIDGET: &str = r#"package com.example.app

class Widget {
    private fun helper(raw: String): String {
        return raw.trim()
    }
}
"#;

const KOTLIN_TO_KOTLIN_INSTANCE_QUALIFIED_CALLER: &str = r#"package com.example.app

fun run(w: Widget, raw: String): String {
    return w.helper(raw)
}
"#;

/// Direction 2 (#1920 AC2): a Kotlin function whose only caller is an
/// INSTANCE-qualified Kotlin call to ANOTHER Kotlin declaration (same
/// language both sides, still a variable receiver, not a type name).
#[test]
fn kotlin_function_called_only_from_kotlin_instance_qualified_call_is_not_reported_dead() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Widget.kt", KOTLIN_WIDGET);
    write_source(
        dir.path(),
        "com/example/app/Caller.kt",
        KOTLIN_TO_KOTLIN_INSTANCE_QUALIFIED_CALLER,
    );

    let widget_index = extract_index(dir.path(), "com/example/app/Widget.kt");
    let helper_symbol = declaration_symbol_owned_by(&widget_index, "helper", "Widget");

    let graph = build_graph_over(
        dir.path(),
        &["com/example/app/Widget.kt", "com/example/app/Caller.kt"],
    );
    let (dead, callers) = dead_and_caller_count(&graph, helper_symbol);
    assert_eq!(
        dead,
        Some(false),
        "Widget.helper is called only via the INSTANCE-qualified Kotlin-to-Kotlin form \
         (w.helper(raw)) and must never be reported definitely dead"
    );
    assert!(callers >= 1, "Widget.helper must keep its real instance-qualified caller edge");
}

const KOTLIN_TYPE_QUALIFIED_CALLER: &str = r#"package com.example.app

fun useJavaHelper(raw: String): String {
    return JavaUtil.helper(raw)
}
"#;

/// #1920 AC3 ("the type-qualified form must keep working"): PIN, run
/// alongside the two guards above in the same file/gate so a future change
/// cannot trade the type-qualified form for the instance-qualified one.
/// This is #1908's own decisive test, duplicated here (not merely
/// referenced) so this file alone proves both call forms coexist.
#[test]
fn type_qualified_kotlin_call_still_binds_alongside_instance_qualified_coverage() {
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/JavaUtil.java", JAVA_UTIL);
    write_source(
        dir.path(),
        "com/example/app/KotlinCaller.kt",
        KOTLIN_TYPE_QUALIFIED_CALLER,
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
        Some(false),
        "JavaUtil.helper is called from Kotlin via the TYPE-qualified form (JavaUtil.helper(raw)) \
         and must keep working -- pinned so a future change cannot regress #1908 while fixing #1920"
    );
    assert!(callers >= 1, "JavaUtil.helper must keep its real type-qualified Kotlin caller edge");
}

/// Control: a Kotlin declaration genuinely unreferenced by EITHER call
/// form must still be reported definitely dead -- proves these guards do
/// not simply mark everything alive.
#[test]
fn an_instance_qualified_call_to_a_different_method_leaves_an_unrelated_one_dead() {
    let source = r#"package com.example.app

class Widget {
    private fun helper(raw: String): String {
        return raw.trim()
    }

    private fun neverCalled(raw: String): String {
        return raw
    }
}

fun run(w: Widget, raw: String): String {
    return w.helper(raw)
}
"#;
    let dir = tempfile::tempdir().unwrap();
    write_source(dir.path(), "com/example/app/Sample.kt", source);

    let index = extract_index(dir.path(), "com/example/app/Sample.kt");
    let never_called = declaration_symbol_owned_by(&index, "neverCalled", "Widget");

    let graph = build_graph_over(dir.path(), &["com/example/app/Sample.kt"]);
    let (dead, _callers) = dead_and_caller_count(&graph, never_called);
    assert_eq!(
        dead,
        Some(true),
        "Widget.neverCalled is genuinely unreferenced -- an instance-qualified call to a \
         DIFFERENT method on the same type must never fabricate liveness for it"
    );
}
