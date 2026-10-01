//! D2 regression coverage: a Java `private` method cannot be called from a
//! different top-level type, but nested types of the same top-level class may
//! access one another's private members.

use xray_core::graph::bind::{bind_with_budget, FileForBind};
use xray_core::graph::budget::{AnalysisCompleteness, IndexBudget};
use xray_core::graph::extract::java::JavaExtractor;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::extract::LanguageExtractor;
use xray_core::graph::identity::SymbolId;

const FILE_ID: u32 = 1;

fn extract_java(source: &str) -> LocalIndex {
    let dir = tempfile::tempdir().expect("create temp dir");
    let path = dir.path().join("Sample.java");
    std::fs::write(&path, source).expect("write fixture source");
    let root = xray_core::scanner::parse_file(&path).expect("fixture source must parse");
    JavaExtractor.extract(&root, FILE_ID)
}

fn method_symbol(index: &LocalIndex, name: &str) -> SymbolId {
    let matches: Vec<_> = index
        .declarations
        .iter()
        .filter(|d| d.name == name)
        .collect();
    match matches.as_slice() {
        [only] => only.symbol,
        [] => panic!("fixture bug: no declaration named {name:?}"),
        other => panic!(
            "fixture bug: {name:?} is ambiguous ({} declarations)",
            other.len()
        ),
    }
}

fn bind(index: LocalIndex) -> xray_core::graph::csr::CodeGraph {
    let graph = bind_with_budget(
        vec![FileForBind {
            file_id: FILE_ID,
            language: "java".to_string(),
            index,
        }],
        &IndexBudget::unlimited(),
    );
    assert_eq!(graph.completeness(), AnalysisCompleteness::Complete);
    graph
}

/// This source is deliberately not javac-valid: the parser must still retain
/// the syntactic call so the graph binder can prove it does not invent an
/// impossible edge to `Other.hidden`. Before D2, unique-name resolution adds
/// exactly that edge and hides this otherwise-dead private method.
#[test]
fn cross_top_level_call_does_not_keep_private_method_live() {
    let source = r#"
class Caller {
    void run() { Other.hidden(); }
}
class Other {
    private static void hidden() {}
}
"#;
    let index = extract_java(source);
    let hidden = method_symbol(&index, "hidden");
    let graph = bind(index);
    let dense = graph.dense_id_for(hidden).expect("hidden must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "a call from another top-level type cannot reference Other.private hidden"
    );
    assert!(
        graph.callers_index(dense).is_empty(),
        "the impossible cross-top-level private call must produce no edge"
    );
}

/// Java grants nested types within the same top-level declaration private
/// access. This positive control guards against treating the extractor's
/// immediate `enclosing_type` names (`Outer` and `Inner`) as separate
/// top-level access domains.
#[test]
fn nested_type_call_keeps_same_top_level_private_method_live() {
    let source = r#"
class Outer {
    private static void hidden() {}
    class Inner {
        void run() { Outer.hidden(); }
    }
}
"#;
    let index = extract_java(source);
    assert!(
        index
            .type_nesting
            .iter()
            .any(|record| { record.type_name == "Outer" && record.top_level_type == "Outer" }),
        "fixture precondition: the outer declaration establishes its own top-level domain"
    );
    assert!(
        index.type_nesting.iter().any(|record| {
            record.type_name == "Inner" && record.top_level_type == "Outer"
        }),
        "empirical extraction proof: Inner has immediate enclosing_type Inner but shares Outer top-level access"
    );
    let hidden = method_symbol(&index, "hidden");
    let graph = bind(index);
    let dense = graph.dense_id_for(hidden).expect("hidden must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "a nested type must retain Java private access within its shared top-level type"
    );
    assert!(
        !graph.callers_index(dense).is_empty(),
        "the same-top-level nested private call must retain a real inbound edge"
    );
}

/// Both prior tests hit the `pool.len() == 1` unique-name shortcut in
/// `resolve_reference`; neither exercises `apply_private_visibility_filter`'s
/// general narrowing path (`pool.len() > 1`), which is what the mission's own
/// production example needs (two same-named private `readObject` methods in
/// two different files, `PerUserPoolDataSource.java` / `SharedPoolDataSource.
/// java`). This fixture reproduces that shape: TWO different top-level
/// classes each declare a same-named private method, and an unrelated third
/// class makes an unqualified call to that name -- Java forbids that call
/// from reaching EITHER one, so neither may gain a false inbound edge.
#[test]
fn ambiguous_same_named_private_methods_in_different_top_level_types_gain_no_false_edge() {
    let source = r#"
class A {
    private void hidden() {}
}
class B {
    private void hidden() {}
}
class Caller {
    void run() { hidden(); }
}
"#;
    let index = extract_java(source);
    let hidden_symbols: Vec<SymbolId> = index
        .declarations
        .iter()
        .filter(|d| d.name == "hidden")
        .map(|d| d.symbol)
        .collect();
    assert_eq!(
        hidden_symbols.len(),
        2,
        "fixture must declare two same-named private methods"
    );
    let graph = bind(index);
    for symbol in hidden_symbols {
        let dense = graph.dense_id_for(symbol).expect("hidden must be interned");
        assert_eq!(
            graph.is_definitely_dead_code(dense),
            Some(true),
            "Caller.run()'s unqualified hidden() cannot reach either A's or B's private \
             method -- neither may gain a false edge from this ambiguous, impossible call"
        );
        assert!(
            graph.callers_index(dense).is_empty(),
            "the impossible ambiguous private call must produce no edge to either candidate"
        );
    }
}

/// An annotation type is a Java top-level declaration just like a class:
/// types nested in its body share one private-access domain. The extractor
/// must therefore establish `Container` as the top-level owner before it
/// walks `A` and `B`; otherwise D2 wrongly treats them as separate types and
/// discards B's legitimate access to A.private hidden.
#[test]
fn types_nested_in_annotation_type_share_private_access_domain() {
    let source = r#"
@interface Container {
    class A {
        private static void hidden() {}
    }
    class B {
        void run() { A.hidden(); }
    }
}
"#;
    let index = extract_java(source);
    // Captured BEFORE `bind()` moves `index`, but asserted AFTER the real
    // behavioural checks below (never before them): a precondition assert
    // placed ahead of the behaviour under test lets a RED run report a
    // fixture-setup failure instead of the actual regression it exists to
    // catch (confirmed live -- removing the annotation_type_declaration
    // dispatch arm made this exact precondition panic first, masking the
    // `is_definitely_dead_code` mismatch entirely). See
    // feedback_tdd_red_must_be_discriminating.md.
    let a_nested_under_container = index
        .type_nesting
        .iter()
        .any(|record| record.type_name == "A" && record.top_level_type == "Container");
    let b_nested_under_container = index
        .type_nesting
        .iter()
        .any(|record| record.type_name == "B" && record.top_level_type == "Container");
    let hidden = method_symbol(&index, "hidden");
    let graph = bind(index);
    let dense = graph.dense_id_for(hidden).expect("hidden must be interned");
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(false),
        "B's same-top-level call must retain A.private hidden"
    );
    assert!(
        !graph.callers_index(dense).is_empty(),
        "the legal nested private call must retain an inbound edge"
    );
    assert!(
        a_nested_under_container,
        "fixture precondition: A must be nested under the annotation type"
    );
    assert!(
        b_nested_under_container,
        "fixture precondition: B must share Container's private-access domain"
    );
}
