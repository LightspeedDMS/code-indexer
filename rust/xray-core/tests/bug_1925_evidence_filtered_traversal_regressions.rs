//! Issue #1925 (epic #1906): "receiver narrowing identifies the right
//! owner but keeps the wrong-owner candidates as edges too". Real
//! javac-valid Java source through the real `JavaExtractor` +
//! `build_repo_graph` front door.
//!
//! Binding design (issue #1924/#1925): #1925 is solved WITHOUT deleting
//! candidates on receiver-type evidence, and WITHOUT a new evidence bit --
//! the EXISTING `RECEIVER_TYPE_MATCH` bit (Story #1806) already
//! distinguishes the right owner from an unrelated same-named sibling; what
//! was missing was a way for an evaluator to ACT on that distinction
//! without a hand-rolled filter loop. This file proves the `GraphHandle`/
//! `CodeGraph` evidence-filtered traversal primitives (`callees_of_
//! filtered`/`callers_of_filtered`/`reachable_to_filtered`) close that gap:
//! the raw graph still fabricates the wrong-owner edge (never deleted),
//! but a `required=RECEIVER_TYPE_MATCH` filter excludes it.
//!
//! Reproduces #1925's own repro shape with neutral names: `Worker.process`
//! takes a `Queue q` parameter (a REPO type) and calls `q.consumeToAny(x)`.
//! `Queue.consumeToAny(String)` is the real target; `Reader.consumeToAny
//! (String)` is an unrelated sibling class sharing the same bare method
//! name, arity, and parameter TYPE (so the pre-existing literal-shape
//! overload narrowing cannot discriminate them either -- only receiver-type
//! evidence can), fabricated into the candidate pool by the bare-name/arity
//! fallback exactly like the issue's `CharacterReader.consumeToAny`
//! reproduction.
//!
//! Neutral naming throughout (`Worker`/`Queue`/`Reader`/`com.example`
//! style) -- no third-party library identifiers, per this repository's
//! Disclosure Discipline.

mod common;

use common::declaration_symbol_owned_by;
use xray_core::graph::csr::CodeGraph;
use xray_core::graph::reasons::RECEIVER_TYPE_MATCH;

const SOURCE: &str = r#"package com.example.app;

public class Worker {
    void process(Queue q) {
        q.consumeToAny("x");
    }
}

class Queue {
    void consumeToAny(String s) {
    }
}

class Reader {
    void consumeToAny(String s) {
    }
}
"#;

/// No evidence bit is forbidden in any of this file's filtered-traversal
/// calls -- named so a reader never mistakes a bare `0` for "no filter at
/// all" (it still REQUIRES `RECEIVER_TYPE_MATCH`; it just forbids nothing
/// in addition).
const NO_FORBIDDEN_EVIDENCE: u16 = 0;
/// Depth bound for `reachable_to_filtered` calls -- this fixture's whole
/// call graph is 1 hop deep, so any value >= 1 suffices; kept generous
/// and named rather than a bare magic literal at each call site.
const MAX_TRAVERSAL_DEPTH: usize = 5;

/// Shared per-test setup: writes the fixture, builds the real graph via
/// `build_repo_graph`, and resolves the three dense ids every test in this
/// file inspects (`Worker.process`, `Queue.consumeToAny`, `Reader.
/// consumeToAny`).
fn setup() -> (CodeGraph, u32, u32, u32) {
    let dir = tempfile::TempDir::new().unwrap();
    common::write_source(dir.path(), "Worker.java", SOURCE);
    let graph = common::build_graph_over(dir.path(), &["Worker.java"]);
    let index = common::extract_index(dir.path(), "Worker.java");

    let caller = declaration_symbol_owned_by(&index, "process", "Worker");
    let caller_dense = graph.dense_id_for(caller).expect("process must be interned");
    let queue_consume = declaration_symbol_owned_by(&index, "consumeToAny", "Queue");
    let queue_dense = graph.dense_id_for(queue_consume).expect("Queue.consumeToAny must be interned");
    let reader_consume = declaration_symbol_owned_by(&index, "consumeToAny", "Reader");
    let reader_dense = graph.dense_id_for(reader_consume).expect("Reader.consumeToAny must be interned");

    (graph, caller_dense, queue_dense, reader_dense)
}

/// Fixture sanity + the bug itself: the UNFILTERED graph must still
/// fabricate BOTH edges (`callees_of` unchanged by the binding design's
/// "never delete" rule), and `edge_evidence` must show `Queue.consumeToAny`
/// carrying `RECEIVER_TYPE_MATCH` while `Reader.consumeToAny` does not --
/// the existing evidence ALREADY distinguishes the right owner, exactly as
/// #1925's own issue text states.
#[test]
fn unfiltered_graph_still_fabricates_both_edges_but_evidence_already_distinguishes_the_real_owner() {
    let (graph, caller_dense, queue_dense, reader_dense) = setup();

    let callees = graph.callees_of(caller_dense);
    assert!(callees.contains(&queue_dense), "Queue.consumeToAny must be a real edge");
    assert!(callees.contains(&reader_dense), "Reader.consumeToAny must ALSO be a real edge -- the #1925 fabrication");

    let queue_evidence = graph.edge_evidence(caller_dense, queue_dense).expect("edge must exist");
    let reader_evidence = graph.edge_evidence(caller_dense, reader_dense).expect("edge must exist");
    assert!(
        queue_evidence & RECEIVER_TYPE_MATCH != 0,
        "Queue.consumeToAny (the real target) must carry RECEIVER_TYPE_MATCH, got {queue_evidence:#06x}"
    );
    assert!(
        reader_evidence & RECEIVER_TYPE_MATCH == 0,
        "Reader.consumeToAny (the unrelated sibling) must NOT carry RECEIVER_TYPE_MATCH, got {reader_evidence:#06x}"
    );
}

/// The fix itself, at the `CodeGraph` level: `callees_of_filtered`/
/// `callers_of_filtered` requiring `RECEIVER_TYPE_MATCH` must return ONLY
/// `Queue.consumeToAny`, excluding `Reader.consumeToAny` -- exactly the
/// "binds only A.m" acceptance criterion from #1925's own issue text.
#[test]
fn callees_and_callers_filtered_by_receiver_type_match_exclude_the_unrelated_sibling() {
    let (graph, caller_dense, queue_dense, reader_dense) = setup();

    let filtered_callees = graph.callees_of_filtered(caller_dense, RECEIVER_TYPE_MATCH, NO_FORBIDDEN_EVIDENCE);
    assert_eq!(filtered_callees, vec![queue_dense], "filtered callees must bind ONLY to Queue.consumeToAny");
    assert!(!filtered_callees.contains(&reader_dense), "the unrelated sibling must be excluded");

    let filtered_callers_of_queue = graph.callers_of_filtered(queue_dense, RECEIVER_TYPE_MATCH, NO_FORBIDDEN_EVIDENCE);
    assert_eq!(filtered_callers_of_queue, vec![caller_dense], "process must be the only filtered caller of Queue.consumeToAny");

    let filtered_callers_of_reader = graph.callers_of_filtered(reader_dense, RECEIVER_TYPE_MATCH, NO_FORBIDDEN_EVIDENCE);
    assert!(
        filtered_callers_of_reader.is_empty(),
        "process must NOT appear as a filtered caller of the unrelated Reader.consumeToAny"
    );
}

/// The same exclusion holds for `reachable_to_filtered` (the blast-radius
/// primitive): a transitive-caller query rooted at `Queue.consumeToAny`
/// with `required=RECEIVER_TYPE_MATCH` must find `process`; the SAME query
/// rooted at `Reader.consumeToAny` must find nothing, since its only
/// inbound edge lacks the required bit.
#[test]
fn reachable_to_filtered_by_receiver_type_match_excludes_the_unrelated_sibling() {
    let (graph, caller_dense, queue_dense, reader_dense) = setup();

    let mut reachable_to_queue =
        graph.reachable_to_filtered(&[queue_dense], MAX_TRAVERSAL_DEPTH, RECEIVER_TYPE_MATCH, NO_FORBIDDEN_EVIDENCE);
    reachable_to_queue.sort_unstable();
    let mut expected = vec![queue_dense, caller_dense];
    expected.sort_unstable();
    assert_eq!(reachable_to_queue, expected, "process must be found as a filtered transitive caller of Queue.consumeToAny");

    let reachable_to_reader =
        graph.reachable_to_filtered(&[reader_dense], MAX_TRAVERSAL_DEPTH, RECEIVER_TYPE_MATCH, NO_FORBIDDEN_EVIDENCE);
    assert_eq!(
        reachable_to_reader,
        vec![reader_dense],
        "Reader.consumeToAny's only inbound edge lacks RECEIVER_TYPE_MATCH -- no filtered caller must be found"
    );
}
