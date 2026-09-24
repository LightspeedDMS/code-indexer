//! Issue #1930 (rework, item 2): the original fix only threaded the
//! extractor's real, AST-walk-tracked `enclosing_method` through for
//! INVOCATION sites (`resolve_all_references`'s `file.index.invocations`
//! loop). `TypeReferenceRecord`/`ConstructionSite` -- the two OTHER
//! reference kinds a single source construct like `new Thing()` produces
//! (a `ConstructionSite` AND a same-line `TypeReferenceRecord` for the
//! type name, alongside its own `InvocationSite` for the constructor
//! call) -- still fell through to the plain `enclosing_symbol` line
//! heuristic, because `resolve_all_references`'s other two loops passed
//! `None` regardless of what the extractor actually recorded. The result:
//! `new Thing()` written in a method AFTER an anonymous class's own
//! method body got a CORRECT invocation edge (from the real enclosing
//! method) but a WRONG construction/type-reference edge (from the
//! anonymous method) -- three edges for one source line disagreeing
//! about who made the call.
//!
//! The fix threads `enclosing_method` onto `TypeReferenceRecord`/
//! `ConstructionSite` too (the extractor already computes it -- see each
//! struct's own doc comment) and routes all three reference kinds
//! through `enclosing_symbol_for_site` uniformly.
//!
//! Every fixture below drives real source through the REAL front door
//! (`build_graph_over`), asserting on `callers_index` dense-id sets and
//! (for the liveness proof) `is_definitely_dead_code`/caller count.
//! Neutral naming (`com.example.app`), per this repository's Disclosure
//! Discipline.

mod common;

use common::{
    build_graph_over, dead_and_caller_count, declaration_symbol, declaration_symbol_owned_by,
    extract_index, write_source,
};
use xray_core::graph::extract::local_index::DeclarationKind;

// =====================================================================
// Java: `new Thing()` written after an anonymous Runnable's own body.
// Its sibling `InvocationSite` (targeting `Thing`'s constructor) was
// ALREADY correctly attributed to `test()` by the original #1930 fix --
// pinned here as a non-regression check. Its `ConstructionSite` and the
// `TypeReferenceRecord` for the same `Thing` mention were NOT.
// =====================================================================

const JAVA_CONSTRUCTION_AFTER_ANON_SOURCE: &str = r#"package com.example.app;

public class Outer {
    void test() {
        Runnable a = new Runnable() { public void run() { helper(); } };
        Thing t = new Thing();
    }
    void helper() {}
}

class Thing {
    Thing() {}
}
"#;

#[test]
fn java_construction_and_type_reference_edges_after_an_anonymous_body_are_attributed_to_the_enclosing_method(
) {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Outer.java",
        JAVA_CONSTRUCTION_AFTER_ANON_SOURCE,
    );

    let index = extract_index(dir.path(), "com/example/app/Outer.java");
    let test_symbol = declaration_symbol_owned_by(&index, "test", "Outer");
    let run_symbol = declaration_symbol(&index, "run");
    let thing_type_symbol = index
        .declarations
        .iter()
        .find(|d| d.name == "Thing" && d.kind == DeclarationKind::Type)
        .expect("fixture bug: Thing's own type declaration must exist")
        .symbol;
    let thing_ctor_symbol = index
        .declarations
        .iter()
        .find(|d| d.name == "Thing" && d.kind == DeclarationKind::Method)
        .expect("fixture bug: Thing's own constructor declaration must exist")
        .symbol;

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.java"]);
    let thing_type_dense = graph.dense_id_for(thing_type_symbol).expect("Thing type must be interned");
    let thing_ctor_dense = graph
        .dense_id_for(thing_ctor_symbol)
        .expect("Thing's constructor must be interned");
    let test_dense = graph.dense_id_for(test_symbol).expect("test must be interned");
    let run_dense = graph.dense_id_for(run_symbol).expect("run must be interned");

    let ctor_callers = graph.callers_index(thing_ctor_dense);
    assert_eq!(
        ctor_callers,
        &[test_dense],
        "non-regression: Thing()'s invocation edge was already correctly attributed to \
         test() by the original #1930 fix, got {ctor_callers:?}"
    );

    let type_callers = graph.callers_index(thing_type_dense);
    assert!(
        !type_callers.contains(&run_dense),
        "Thing's construction/type-reference edges must NEVER be attributed to the \
         anonymous run() method -- that call happens after its body ends, got \
         {type_callers:?}"
    );
    assert!(
        type_callers.contains(&test_dense),
        "Thing's construction/type-reference edges must be attributed to test(), the real \
         enclosing method, got {type_callers:?}"
    );

    // Liveness proof (Issue #1930 rework item 2's own requirement): this
    // fix only changes WHO the caller is, never WHETHER Thing is
    // referenced at all -- it must never flip to a dead verdict.
    let (dead, callers) = dead_and_caller_count(&graph, thing_type_symbol);
    assert_ne!(dead, Some(true), "Thing must never be reported definitely dead");
    assert!(callers >= 1, "Thing must keep at least one real caller edge");
}

// =====================================================================
// Kotlin: `KThing()` written after a local function's own body.
// =====================================================================

const KOTLIN_CONSTRUCTION_AFTER_LOCAL_FUN_SOURCE: &str = r#"package com.example.app

class Outer {
    fun test() {
        fun localFun() {
            helper()
        }
        localFun()
        KThing()
    }
    fun helper() {}
}

class KThing
"#;

#[test]
fn kotlin_construction_edge_after_a_local_function_is_attributed_to_the_enclosing_method() {
    let dir = tempfile::tempdir().unwrap();
    write_source(
        dir.path(),
        "com/example/app/Outer.kt",
        KOTLIN_CONSTRUCTION_AFTER_LOCAL_FUN_SOURCE,
    );

    let index = extract_index(dir.path(), "com/example/app/Outer.kt");
    let test_symbol = declaration_symbol_owned_by(&index, "test", "Outer");
    let local_fun_symbol = declaration_symbol_owned_by(&index, "localFun", "Outer");
    let kthing_symbol = declaration_symbol(&index, "KThing");

    let graph = build_graph_over(dir.path(), &["com/example/app/Outer.kt"]);
    let kthing_dense = graph.dense_id_for(kthing_symbol).expect("KThing must be interned");
    let test_dense = graph.dense_id_for(test_symbol).expect("test must be interned");
    let local_fun_dense = graph
        .dense_id_for(local_fun_symbol)
        .expect("localFun must be interned");

    let kthing_callers = graph.callers_index(kthing_dense);
    assert!(
        !kthing_callers.contains(&local_fun_dense),
        "Kotlin: KThing()'s construction edge must NEVER be attributed to localFun() -- that \
         call happens after localFun()'s own body ends, got {kthing_callers:?}"
    );
    assert_eq!(
        kthing_callers,
        &[test_dense],
        "Kotlin: KThing()'s construction edge must be attributed to test(), the real \
         enclosing function, got {kthing_callers:?}"
    );

    let (dead, callers) = dead_and_caller_count(&graph, kthing_symbol);
    assert_ne!(dead, Some(true), "KThing must never be reported definitely dead");
    assert!(callers >= 1, "KThing must keep at least one real caller edge");
}
