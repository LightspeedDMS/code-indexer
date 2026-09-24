//! Issue #1924 (epic #1906): end-to-end regression coverage for
//! `receiver_mismatch::apply_receiver_type_mismatch_tagging`, driving real
//! javac-valid Java source through the real `JavaExtractor` +
//! `build_repo_graph` front door (no hand-built `LocalIndex`, no mocking)
//! -- the same style `bug_1922_type_qualifier_regressions.rs`/`bug_1899_
//! cycle_precision_regressions.rs` already use for this binder's narrowing
//! passes.
//!
//! Binding design (issue #1924/#1925, applies to both): this is solved
//! WITHOUT deleting candidates on receiver-type evidence -- every edge these
//! fixtures reproduce MUST still exist (`callees_of` unchanged), only carry
//! a NEW evidence bit (`RECEIVER_TYPE_MISMATCH`) an evaluator can filter on
//! via `GraphHandle`'s evidence-filtered traversal primitives (see
//! `bug_1925_evidence_filtered_traversal_regressions.rs`).
//!
//! Neutral naming throughout (`Worker`/`Helper`/`Other`/`com.example`
//! style) -- no third-party library identifiers, per this repository's
//! Disclosure Discipline.

mod common;

use common::declaration_symbol_owned_by;

const SOURCE: &str = r#"package com.example.app;

public class Worker {
    String stringReceiver(String key) {
        // #1924: `key`'s declared type is a genuine, POSITIVE, sourced
        // TypedNameRecord hit (a real method parameter), and `String` is
        // closed-world -- no repo type can ever be a subtype of it.
        return key.equals("x") ? "a" : "b";
    }

    String repoTypedReceiver(Helper h) {
        // `Helper` is a REPO type, never closed-world -- must NOT be tagged
        // RECEIVER_TYPE_MISMATCH, regardless of which candidate is reached.
        return h.equals("x") ? "a" : "b";
    }

    String unknownReceiver() {
        // `getClass()` is `Object.getClass()` -- never declared anywhere in
        // this repo, so `return_type_of_method_on_type` finds no repo
        // declaration to read a return type off at all. A genuinely
        // javac-valid call (every Java object inherits `getClass()`) whose
        // receiver type is UNRESOLVABLE by this binder's own (extraction-
        // only, no JDK method table) evidence -- exactly the "unknown
        // receiver" case: `resolve_receiver_type` returns
        // ReceiverEvidence::None.
        return getClass().equals("x") ? "a" : "b";
    }
}

class Helper {
    public boolean equals(Object o) {
        return false;
    }
}

class Other {
    public boolean equals(Object o) {
        return false;
    }
}
"#;

fn write_fixture(dir: &std::path::Path) {
    common::write_source(dir, "Worker.java", SOURCE);
}

/// Resolves the dense ids of `Worker.<caller_method>`, `Helper.equals`, and
/// `Other.equals` for one built graph -- shared by all three tests below,
/// which differ only in which caller method they inspect and what they
/// expect the evidence to say.
fn resolve_triple(
    graph: &xray_core::graph::csr::CodeGraph,
    index: &xray_core::graph::extract::local_index::LocalIndex,
    caller_method: &str,
) -> (u32, u32, u32) {
    let caller = declaration_symbol_owned_by(index, caller_method, "Worker");
    let caller_dense = graph.dense_id_for(caller).unwrap_or_else(|| panic!("{caller_method} must be interned"));
    let helper_equals = declaration_symbol_owned_by(index, "equals", "Helper");
    let helper_dense = graph.dense_id_for(helper_equals).expect("Helper.equals must be interned");
    let other_equals = declaration_symbol_owned_by(index, "equals", "Other");
    let other_dense = graph.dense_id_for(other_equals).expect("Other.equals must be interned");
    (caller_dense, helper_dense, other_dense)
}

/// #1924's own repro shape: `key.equals("x")` where `key: String` binds, by
/// bare-name/arity fallback, to EVERY in-repo `equals(Object)` override --
/// both `Helper.equals` and `Other.equals` must still be real edges
/// (`callees_of` unchanged, per the binding design), but BOTH must now
/// carry `RECEIVER_TYPE_MISMATCH` in their `edge_evidence`.
#[test]
fn string_receiver_equals_call_tags_every_repo_owned_candidate_as_mismatched_but_keeps_every_edge() {
    let dir = tempfile::TempDir::new().unwrap();
    write_fixture(dir.path());
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");
    let (caller_dense, helper_dense, other_dense) = resolve_triple(&graph, &index, "stringReceiver");

    let callees = graph.callees_of(caller_dense);
    assert!(callees.contains(&helper_dense), "Helper.equals must still be a real edge -- never deleted");
    assert!(callees.contains(&other_dense), "Other.equals must still be a real edge -- never deleted");

    for (label, target_dense) in [("Helper.equals", helper_dense), ("Other.equals", other_dense)] {
        let evidence = graph.edge_evidence(caller_dense, target_dense).expect("edge must exist");
        assert!(
            evidence & xray_core::graph::reasons::RECEIVER_TYPE_MISMATCH != 0,
            "{label} reached via a String receiver must carry RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
        );
    }
}

/// A repo-typed receiver (`Helper h; h.equals(...)`) must NEVER carry
/// `RECEIVER_TYPE_MISMATCH` on ANY candidate (`Helper.equals` -- the real
/// owner match -- or `Other.equals`, the same-arity sibling still fabricated
/// by the bare-name/arity fallback) -- `Helper` is an ordinary open-world
/// repo type, not closed-world, so this binder has no sound basis to claim
/// any candidate is provably unrelated to it.
#[test]
fn repo_typed_receiver_equals_call_never_tags_any_candidate_as_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    write_fixture(dir.path());
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");
    let (caller_dense, helper_dense, other_dense) = resolve_triple(&graph, &index, "repoTypedReceiver");

    let callees = graph.callees_of(caller_dense);
    assert!(callees.contains(&helper_dense), "Helper.equals must still be a real edge -- never deleted");
    assert!(callees.contains(&other_dense), "Other.equals must still be a real edge -- never deleted");

    for (label, target_dense) in [("Helper.equals", helper_dense), ("Other.equals", other_dense)] {
        let evidence = graph.edge_evidence(caller_dense, target_dense).expect("edge must exist");
        assert!(
            evidence & xray_core::graph::reasons::RECEIVER_TYPE_MISMATCH == 0,
            "{label} reached via a repo-typed receiver must never carry RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
        );
    }
    // The genuine positive-owner match must still be tagged, unaffected.
    let helper_evidence = graph.edge_evidence(caller_dense, helper_dense).expect("Helper.equals edge must exist");
    assert!(
        helper_evidence & xray_core::graph::reasons::RECEIVER_TYPE_MATCH != 0,
        "Helper.equals must still carry RECEIVER_TYPE_MATCH for its own real receiver type"
    );
}

/// A genuinely UNKNOWN receiver (`getClass()` -- `Object.getClass()`, never
/// declared in-repo, so its return type cannot be resolved at all) must
/// leave every candidate untouched by this pass -- `apply_receiver_type_
/// mismatch_tagging` must be a complete no-op on both `Helper.equals` and
/// `Other.equals`.
#[test]
fn unknown_receiver_equals_call_never_tags_any_candidate_as_mismatched() {
    let dir = tempfile::TempDir::new().unwrap();
    write_fixture(dir.path());
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");
    let (caller_dense, helper_dense, other_dense) = resolve_triple(&graph, &index, "unknownReceiver");

    let callees = graph.callees_of(caller_dense);
    assert!(callees.contains(&helper_dense), "Helper.equals must still be a real edge -- fabricated by bare-name/arity fallback");
    assert!(callees.contains(&other_dense), "Other.equals must still be a real edge -- fabricated by bare-name/arity fallback");

    for (label, target_dense) in [("Helper.equals", helper_dense), ("Other.equals", other_dense)] {
        let evidence = graph.edge_evidence(caller_dense, target_dense).expect("edge must exist");
        assert!(
            evidence & xray_core::graph::reasons::RECEIVER_TYPE_MISMATCH == 0,
            "{label} reached via an unknown receiver must never carry RECEIVER_TYPE_MISMATCH, got {evidence:#06x}"
        );
    }
}
