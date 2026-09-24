//! Second half of `code_graph.rs`'s relocated unit tests -- see
//! `code_graph_tests.rs`'s own module doc for the full split rationale.
//! THIS file holds the `edge_reason`/`edge_evidence`/`location_for`
//! coverage, plus the `callees_of_filtered`/`callers_of_filtered`
//! (#1924/#1925) coverage added since the original relocation.

use super::super::builder::CodeGraphBuilder;
use super::super::candidate::Candidate;
use super::EdgeReason;
use crate::graph::identity::make_symbol_id;
use crate::graph::reasons;

/// Bug #1900 (epic #1906 P2, inherited from #1899's AC): the CENTRAL
/// discriminating test for `edge_reason`. `caller` has two outbound
/// references: one resolved to a SINGLE surviving candidate
/// (`unambiguous_target`), and one whose window held TWO surviving
/// candidates (`ambiguous_target_a`/`_b`). A caller filtering a graph
/// finding (e.g. an SCC or a reachability path) to trustworthy edges
/// needs to tell these apart -- this is the exact mechanism the
/// cycle-precision bug (#1899) asks for. `RED against unmodified code`:
/// neither `CodeGraph::edge_reason` nor `EdgeReason` exist yet, so this
/// fails to compile -- once implemented, a wrong implementation (e.g.
/// always reporting `Ambiguous`, or never distinguishing by window
/// size) would still fail these specific assertions.
#[test]
fn edge_reason_distinguishes_unambiguous_single_candidate_from_ambiguous_multi_candidate_edges() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
    let caller = builder.intern_symbol(make_symbol_id(1, 0));
    let unambiguous_target = builder.intern_symbol(make_symbol_id(1, 1));
    let ambiguous_target_a = builder.intern_symbol(make_symbol_id(1, 2));
    let ambiguous_target_b = builder.intern_symbol(make_symbol_id(1, 3));

    // Reference 0: caller -> unambiguous_target, exactly ONE candidate.
    builder.add_reference(caller, 1, 10, 0, &[Candidate::new(unambiguous_target, reasons::UNIQUE_NAME_IN_REPO)]);
    // Reference 1: caller -> {ambiguous_target_a, ambiguous_target_b}, TWO candidates.
    builder.add_reference(
        caller,
        1,
        11,
        0,
        &[
            Candidate::new(ambiguous_target_a, reasons::SAME_PACKAGE),
            Candidate::new(ambiguous_target_b, reasons::SAME_PACKAGE),
        ],
    );
    let graph = builder.build();

    assert_eq!(
        graph.edge_reason(caller, unambiguous_target),
        Some(EdgeReason::SoleCandidate),
        "a single-candidate reference window must report SoleCandidate"
    );
    assert_eq!(
        graph.edge_reason(caller, ambiguous_target_a),
        Some(EdgeReason::MultipleCandidates),
        "a multi-candidate reference window must report MultipleCandidates for every candidate in it"
    );
    assert_eq!(graph.edge_reason(caller, ambiguous_target_b), Some(EdgeReason::MultipleCandidates));
    assert_eq!(
        graph.edge_reason(caller, 999),
        None,
        "a pair with no edge at all must report None, never a fabricated tier"
    );
}

/// A single symbol pair reached by BOTH an ambiguous and an
/// unambiguous reference must report `Unambiguous` -- real, positive
/// single-target evidence from ONE call site is never invalidated by a
/// separate, weaker call site that also happens to target the same
/// symbol.
#[test]
fn edge_reason_prefers_unambiguous_when_the_same_pair_has_both_kinds_of_evidence() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
    let caller = builder.intern_symbol(make_symbol_id(2, 0));
    let target = builder.intern_symbol(make_symbol_id(2, 1));
    let other = builder.intern_symbol(make_symbol_id(2, 2));

    // Reference 0: caller -> {target, other}, ambiguous.
    builder.add_reference(
        caller,
        2,
        5,
        0,
        &[Candidate::new(target, reasons::SAME_PACKAGE), Candidate::new(other, reasons::SAME_PACKAGE)],
    );
    // Reference 1: caller -> target, unambiguous.
    builder.add_reference(caller, 2, 6, 0, &[Candidate::new(target, reasons::UNIQUE_NAME_IN_REPO)]);
    let graph = builder.build();

    assert_eq!(
        graph.edge_reason(caller, target),
        Some(EdgeReason::SoleCandidate),
        "one sole-candidate call site is real evidence, regardless of a separate multi-candidate one"
    );
}

/// Bug #1900 (epic #1906 P2, review round 2 -- the CENTRAL discriminating
/// test for the fabricated-edge fix, at the `CodeGraph` level): a
/// `SoleCandidate` edge (per `edge_reason`) can still carry weak
/// evidence only -- `edge_evidence` must report the candidate's REAL
/// `reasons()` bitmask, never derive anything from the candidate count.
/// Reproduces the review's own `java.util.Map.put` shape: a single
/// surviving candidate resolved via `SAME_PACKAGE`/`ARITY_MATCH` alone,
/// with no `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO`.
#[test]
fn edge_evidence_reports_weak_evidence_for_a_sole_candidate_edge() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
    let caller = builder.intern_symbol(make_symbol_id(5, 0));
    let fabricated_target = builder.intern_symbol(make_symbol_id(5, 1));

    // A single surviving candidate (edge_reason == SoleCandidate) whose
    // ONLY evidence is weak structural matching -- the exact shape the
    // review proved gets mislabeled by cand_len alone.
    builder.add_reference(
        caller,
        5,
        1,
        0,
        &[Candidate::new(fabricated_target, reasons::SAME_PACKAGE | reasons::ARITY_MATCH)],
    );
    let graph = builder.build();

    assert_eq!(
        graph.edge_reason(caller, fabricated_target),
        Some(EdgeReason::SoleCandidate),
        "fixture sanity: this is exactly the count-based tier a fabricated single candidate reports"
    );
    assert_eq!(
        graph.edge_evidence(caller, fabricated_target),
        Some(reasons::SAME_PACKAGE | reasons::ARITY_MATCH),
        "edge_evidence must report the candidate's REAL reasons bitmask, not a count-derived flag"
    );
    assert_eq!(
        graph.edge_evidence(caller, fabricated_target).unwrap() & reasons::RECEIVER_TYPE_MATCH,
        0,
        "the fabricated edge carries no RECEIVER_TYPE_MATCH bit -- a caller checking for \
         strong evidence can now tell this apart from a genuinely verified hop"
    );
}

/// Bug #1900 (review round 2, reviewer-relay finding): a single
/// pre-combined candidate cannot distinguish "OR of every contributing
/// occurrence" from "just returns the first/only match's reasons" -- an
/// implementation that picked ONE candidate's reasons rather than
/// OR-ing across occurrences would still pass the test above. This test
/// uses TWO SEPARATE references from the same caller to the same
/// target, each carrying a DIFFERENT, non-overlapping reason bit, and
/// asserts `edge_evidence` reports the bitwise UNION of both -- the
/// only way that union can appear is if both occurrences were actually
/// combined.
#[test]
fn edge_evidence_ors_bits_across_two_separate_references_to_the_same_target() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
    let caller = builder.intern_symbol(make_symbol_id(6, 0));
    let target = builder.intern_symbol(make_symbol_id(6, 1));

    // Reference 0: caller -> target, evidence bit A only.
    builder.add_reference(caller, 6, 1, 0, &[Candidate::new(target, reasons::SAME_PACKAGE)]);
    // Reference 1: a SEPARATE call site, caller -> target again, evidence bit B only.
    builder.add_reference(caller, 6, 2, 0, &[Candidate::new(target, reasons::UNIQUE_NAME_IN_REPO)]);
    let graph = builder.build();

    assert_eq!(
        graph.edge_evidence(caller, target),
        Some(reasons::SAME_PACKAGE | reasons::UNIQUE_NAME_IN_REPO),
        "edge_evidence must be the OR of BOTH occurrences' reason bits, not either one alone"
    );
}

#[test]
fn edge_evidence_returns_none_for_a_pair_with_no_edge() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
    let caller = builder.intern_symbol(make_symbol_id(7, 0));
    let target = builder.intern_symbol(make_symbol_id(7, 1));
    builder.add_reference(caller, 7, 1, 0, &[Candidate::new(target, reasons::SAME_FILE)]);
    let graph = builder.build();

    assert_eq!(
        graph.edge_evidence(caller, 999),
        None,
        "a pair with no edge at all must report None, never a fabricated Some(0)"
    );
}

/// Bug #1900 (epic #1906 P2/P5): `location_for` must report a
/// DECLARATION's own file+line -- captured via `add_location`,
/// independent of any `Reference`'s call-site coordinates -- and must
/// return `None`, never fabricate one, for a symbol with no recorded
/// location. `RED against unmodified code`: `CodeGraphBuilder` has no
/// `add_location` method yet, so this fails to compile.
#[test]
fn location_for_returns_the_declared_file_and_line_and_none_when_absent() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let with_location = builder.intern_symbol(make_symbol_id(4, 0));
    let without_location = builder.intern_symbol(make_symbol_id(4, 1));
    let file_string_id = builder.intern_string("com/example/Foo.java");
    builder.add_location(with_location, file_string_id, 42);
    let graph = builder.build();

    assert_eq!(
        graph.location_for(with_location),
        Some(("com/example/Foo.java", 42)),
        "location_for must resolve the interned file path and the exact recorded line"
    );
    assert_eq!(
        graph.location_for(without_location),
        None,
        "a symbol never passed to add_location must return None, never a fabricated location"
    );
}

/// #1924/#1925 (epic #1906): `callees_of_filtered` must include a callee
/// reached by an occurrence satisfying the required/forbidden mask and
/// exclude one that is not, while `callees_of` (unfiltered) keeps returning
/// BOTH -- the binding design's own "never delete, only let an evaluator
/// filter" contract. `caller` has two callees: `matched` (RECEIVER_TYPE_
/// MATCH, no MISMATCH) and `mismatched` (RECEIVER_TYPE_MATCH | RECEIVER_
/// TYPE_MISMATCH, the #1924 fabricated-edge shape).
#[test]
fn callees_of_filtered_returns_only_targets_whose_evidence_satisfies_the_filter() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
    let caller = builder.intern_symbol(make_symbol_id(10, 0));
    let matched = builder.intern_symbol(make_symbol_id(10, 1));
    let mismatched = builder.intern_symbol(make_symbol_id(10, 2));

    builder.add_reference(caller, 10, 1, 0, &[Candidate::new(matched, reasons::RECEIVER_TYPE_MATCH)]);
    builder.add_reference(
        caller,
        10,
        2,
        0,
        &[Candidate::new(mismatched, reasons::RECEIVER_TYPE_MATCH | reasons::RECEIVER_TYPE_MISMATCH)],
    );
    let graph = builder.build();

    let mut unfiltered = graph.callees_of(caller);
    unfiltered.sort_unstable();
    let mut expected_unfiltered = vec![matched, mismatched];
    expected_unfiltered.sort_unstable();
    assert_eq!(unfiltered, expected_unfiltered, "unfiltered callees_of must keep BOTH edges -- never delete");

    let filtered = graph.callees_of_filtered(caller, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH);
    assert_eq!(filtered, vec![matched], "filtered must exclude the mismatched candidate, keep the matched one");

    let mut everything = graph.callees_of_filtered(caller, 0, 0);
    everything.sort_unstable();
    assert_eq!(everything, expected_unfiltered, "an empty required/forbidden mask must exclude nothing");
}

/// #1924/#1925: `callers_of_filtered` must derive its filter from the
/// LAZILY-BUILT `reverse_evidence_index` (the plain
/// `reverse_index` still carries no evidence of its own) -- `target` has
/// two callers, `good_caller` (RECEIVER_TYPE_MATCH only) and `bad_caller`
/// (RECEIVER_TYPE_MATCH | RECEIVER_TYPE_MISMATCH); unfiltered `callers_of`
/// must keep both, filtered must keep only `good_caller`.
#[test]
fn callers_of_filtered_derives_evidence_from_the_lazy_reverse_evidence_index() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
    let target = builder.intern_symbol(make_symbol_id(11, 0));
    let good_caller = builder.intern_symbol(make_symbol_id(11, 1));
    let bad_caller = builder.intern_symbol(make_symbol_id(11, 2));

    builder.add_reference(good_caller, 11, 1, 0, &[Candidate::new(target, reasons::RECEIVER_TYPE_MATCH)]);
    builder.add_reference(
        bad_caller,
        11,
        2,
        0,
        &[Candidate::new(target, reasons::RECEIVER_TYPE_MATCH | reasons::RECEIVER_TYPE_MISMATCH)],
    );
    let graph = builder.build();

    let mut unfiltered = graph.callers_of(target);
    unfiltered.sort_unstable();
    let mut expected_unfiltered = vec![good_caller, bad_caller];
    expected_unfiltered.sort_unstable();
    assert_eq!(unfiltered, expected_unfiltered, "unfiltered callers_of must keep BOTH callers -- never delete");

    let filtered = graph.callers_of_filtered(target, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH);
    assert_eq!(filtered, vec![good_caller], "filtered must exclude the mismatched caller, keep the good one");

    assert!(
        graph.callers_of_filtered(target, reasons::RECEIVER_TYPE_MISMATCH, 0).contains(&bad_caller),
        "requiring RECEIVER_TYPE_MISMATCH must still find bad_caller -- proves this is a real filter, not a stub"
    );
}

const PERF_CALLER_COUNT: u32 = 1000;
const PERF_FANOUT: u32 = 1000;
const PERF_QUERY_COUNT: u32 = 50;
const PERF_FILE_ID: u32 = 100;
const PERF_LINE: u32 = 1;

/// Builds a HUB target with `PERF_CALLER_COUNT` callers, each ALSO fanning
/// out to `PERF_FANOUT` other distinct targets -- a substantial forward
/// out-degree per caller, exactly the shape that made the pre-rework
/// `callers_of_filtered` scan expensive. Returns `(graph, hub)`.
fn build_hub_fanout_fixture() -> (super::super::code_graph::CodeGraph, u32) {
    let mut builder = CodeGraphBuilder::with_candidate_capacity((PERF_CALLER_COUNT * (PERF_FANOUT + 1)) as usize);
    let hub = builder.intern_symbol(make_symbol_id(PERF_FILE_ID, 0));
    let mut next_local = 1u32;
    for _ in 0..PERF_CALLER_COUNT {
        let caller = builder.intern_symbol(make_symbol_id(PERF_FILE_ID, next_local));
        next_local += 1;
        builder.add_reference(caller, PERF_FILE_ID, PERF_LINE, 0, &[Candidate::new(hub, reasons::RECEIVER_TYPE_MATCH)]);
        next_local += 1;
        for _ in 0..PERF_FANOUT {
            let other_target = builder.intern_symbol(make_symbol_id(PERF_FILE_ID, next_local));
            next_local += 1;
            builder.add_reference(caller, PERF_FILE_ID, PERF_LINE, 0, &[Candidate::new(other_target, reasons::SAME_FILE)]);
            next_local += 1;
        }
    }
    (builder.build(), hub)
}

/// #1924/#1925 (P3): work-count (never wall-clock) proof of the lazy
/// reverse-evidence design's two complexity claims, using the test-only
/// `CodeGraph::reverse_evidence_build_count`/`AdjacencyIndex::scan_count`
/// counters:
///
/// 1. **Built at most once.** `reverse_evidence_build_count` must read
///    EXACTLY 1 after `PERF_QUERY_COUNT` repeated `callers_of_filtered`
///    calls on the SAME `graph` -- the `OnceLock`'s closure runs on the
///    first call only, never rebuilding per query.
/// 2. **O(in-degree) per query.** `scan_count` must read EXACTLY
///    `PERF_QUERY_COUNT * PERF_CALLER_COUNT` -- each query examines only
///    `hub`'s own `PERF_CALLER_COUNT`-sized reverse CSR range, NEVER any
///    caller's own `PERF_FANOUT`-sized forward out-degree slice (which
///    would inflate this count by a further factor of `PERF_FANOUT`,
///    i.e. to `PERF_QUERY_COUNT * PERF_CALLER_COUNT * PERF_FANOUT` --
///    the pre-rework algorithm's cost shape).
#[test]
fn callers_of_filtered_builds_the_lazy_index_once_and_scans_only_in_degree_per_query() {
    let (graph, hub) = build_hub_fanout_fixture();

    for _ in 0..PERF_QUERY_COUNT {
        let result = graph.callers_of_filtered(hub, reasons::RECEIVER_TYPE_MATCH, 0);
        assert_eq!(result.len(), PERF_CALLER_COUNT as usize, "every caller must be found");
    }

    let build_count = graph.reverse_evidence_build_count.load(std::sync::atomic::Ordering::SeqCst);
    assert_eq!(
        build_count, 1,
        "the lazy reverse-evidence index must be built EXACTLY ONCE across {PERF_QUERY_COUNT} \
         repeated queries, not once per query"
    );

    let scanned = graph
        .reverse_evidence_index
        .get()
        .expect("built by the loop above")
        .scan_count();
    let expected_scanned = (PERF_QUERY_COUNT * PERF_CALLER_COUNT) as usize;
    assert_eq!(
        scanned, expected_scanned,
        "each of {PERF_QUERY_COUNT} queries must scan EXACTLY hub's own in-degree \
         ({PERF_CALLER_COUNT}) occurrences -- a total other than {expected_scanned} means some \
         query touched more than its own CSR range (e.g. a caller's {PERF_FANOUT}-sized forward \
         out-degree slice, the pre-rework cost shape)"
    );
}

/// Bug #1900 (epic #1906 P2, review round 2 -- BLOCKING P3): a corrupt
/// `file_string_id` (out of range for this graph's string table) must
/// make `location_for` return `None`, never panic. `thunk_location_for_
/// raw` is an FFI thunk reached from a dylib-supplied `GraphHandle` on
/// caller-controlled input; ADR-002 Defect 2 exists precisely so a
/// panic can never cross that boundary, exactly like
/// `try_resolve_symbol`/`try_resolve_string` already guarantee for
/// every other GraphHandle-reachable resolution. This builds the
/// corrupt state directly via `add_location` (bypassing `intern_string`
/// entirely) -- the same shape a corrupt `--graph-in` file would
/// produce if it ever slipped past `read_locations`'s own wire-level
/// validation (see `wire.rs`). `RED against the pre-fix code`: the old
/// `self.strings.resolve(file_string_id)` panics here instead of
/// returning `None`.
#[test]
fn location_for_returns_none_instead_of_panicking_for_a_corrupt_file_string_id() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let symbol = builder.intern_symbol(make_symbol_id(9, 0));
    const OUT_OF_RANGE_STRING_ID: u32 = 999;
    builder.add_location(symbol, OUT_OF_RANGE_STRING_ID, 1);
    let graph = builder.build();

    assert_eq!(graph.location_for(symbol), None, "an out-of-range file_string_id must return None, never panic");
}
