//! `code_graph.rs`'s unit tests, split into their own file (Messi Rule 6,
//! anti-file-bloat) so `code_graph.rs` itself stays under the project's
//! 1000-line limit -- mirrors the split `receiver.rs`/`receiver_tests.rs`
//! already use, wired via `#[cfg(test)] #[path = "code_graph_tests.rs"]
//! mod tests;`. This file itself is a pure relocation, no content changed.
//! Split further, across two sibling files, to stay under a 500-line-per-
//! file review threshold: THIS file holds the round-trip/completeness/
//! kind/visibility coverage; `code_graph_edge_tests.rs` holds the `edge_
//! reason`/`edge_evidence`/`location_for` coverage (plus new #1924/#1925
//! tests added since the original relocation -- see that file's own doc
//! comment).

use super::super::builder::CodeGraphBuilder;
use super::super::candidate::Candidate;
use crate::graph::bind::depth::BinderDepth;
use crate::graph::identity::make_symbol_id;
use crate::graph::reasons;

/// End-to-end wiring test: build a small graph through the real
/// builder, then verify every query surface (`references`,
/// `candidates_for`, `resolve_symbol`, `resolve_string`,
/// `is_ambiguous`/`is_unresolved` read through the graph) agrees with
/// what was built -- not just each piece in isolation.
#[test]
fn graph_round_trips_references_candidates_symbols_and_strings() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(3);

    let foo_symbol = make_symbol_id(1, 0);
    let bar_symbol = make_symbol_id(1, 1);
    let foo_dense = builder.intern_symbol(foo_symbol);
    let bar_dense = builder.intern_symbol(bar_symbol);
    let foo_name = builder.intern_string("Foo");

    // Reference 0: ambiguous call resolved to two candidates.
    builder.add_reference(
        10,
        1,
        5,
        0,
        &[
            Candidate::new(foo_dense, reasons::SAME_FILE),
            Candidate::new(bar_dense, reasons::SAME_PACKAGE),
        ],
    );
    // Reference 1: exact, single candidate.
    builder.add_reference(11, 1, 6, 0, &[Candidate::new(foo_dense, reasons::UNIQUE_NAME_IN_REPO)]);
    // Reference 2: out-of-repo call, empty candidate set.
    builder.add_reference(12, 1, 7, 0, &[]);

    builder.set_binder_depths(vec![BinderDepth::new("java")]);

    let graph = builder.build();

    assert_eq!(graph.references().len(), 3);
    assert_eq!(graph.binder_depths(), &[BinderDepth::new("java")]);

    let ref0 = graph.references()[0];
    assert!(ref0.is_ambiguous());
    assert!(!ref0.is_unresolved());
    let ref0_candidates = graph.candidates_for(&ref0);
    assert_eq!(ref0_candidates.len(), 2);
    assert_eq!(graph.resolve_symbol(ref0_candidates[0].symbol()), foo_symbol);
    assert_eq!(graph.resolve_symbol(ref0_candidates[1].symbol()), bar_symbol);

    let ref1 = graph.references()[1];
    assert!(!ref1.is_ambiguous());
    assert!(!ref1.is_unresolved());
    assert_eq!(graph.candidates_for(&ref1).len(), 1);

    let ref2 = graph.references()[2];
    assert!(ref2.is_unresolved());
    assert!(graph.candidates_for(&ref2).is_empty());

    assert_eq!(graph.resolve_string(foo_name), "Foo");
}

/// AC7: `strongly_connected_components` (in `super::ops`) needs to
/// enumerate every interned symbol's dense id from OUTSIDE this
/// module, where `self.symbols` is private -- this accessor is the
/// seam that lets it do so without exposing the `SymbolTable` type
/// itself.
#[test]
fn symbol_count_reports_the_number_of_distinct_interned_symbols() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    builder.intern_symbol(make_symbol_id(1, 0));
    builder.intern_symbol(make_symbol_id(1, 1));
    // Interning the SAME symbol again must not inflate the count.
    builder.intern_symbol(make_symbol_id(1, 0));

    let graph = builder.build();
    assert_eq!(graph.symbol_count(), 2);
}

/// AC7: `graph::csr::wire::write_graph_file` (next) needs to enumerate
/// every interned string from OUTSIDE this module (where `self.strings`
/// is private) to serialize the mmap-handoff wire format -- this
/// accessor is that seam, mirroring `symbol_count` above exactly.
#[test]
fn string_count_reports_the_number_of_distinct_interned_strings() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    builder.intern_string("Foo");
    builder.intern_string("Bar");
    // Interning the SAME string again must not inflate the count.
    builder.intern_string("Foo");

    let graph = builder.build();
    assert_eq!(graph.string_count(), 2);
}

/// AC6: `CodeGraphBuilder` records the completeness state, the
/// decoupled referenced-bit, and a per-symbol cached signature line;
/// `CodeGraph` surfaces all three as real query methods, plus a
/// reverse `dense_id_for` lookup and the `is_definitely_dead_code`
/// dead-code-tier gate AC6 requires.
#[test]
fn budget_outcome_fields_round_trip_through_the_builder() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let live_symbol = make_symbol_id(1, 0);
    let dead_symbol = make_symbol_id(1, 1);
    let live_dense = builder.intern_symbol(live_symbol);
    let dead_dense = builder.intern_symbol(dead_symbol);

    builder.mark_referenced(live_dense);
    builder.add_signature(live_dense, "run()".to_string());
    builder.set_completeness(crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);

    let graph = builder.build();

    assert_eq!(graph.completeness(), crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);
    assert!(graph.is_symbol_referenced(live_dense));
    assert!(!graph.is_symbol_referenced(dead_dense));
    assert_eq!(graph.signature_for(live_dense), Some("run()"));
    assert_eq!(graph.signature_for(dead_dense), None);
    assert_eq!(graph.dense_id_for(live_symbol), Some(live_dense));
    assert_eq!(graph.dense_id_for(make_symbol_id(9, 9)), None);

    // Referenced -> never dead, regardless of completeness.
    assert_eq!(graph.is_definitely_dead_code(live_dense), Some(false));
    // Unreferenced + IndexBudgetExceeded -> suppressed (None), never
    // a false "definitely dead" verdict.
    assert_eq!(graph.is_definitely_dead_code(dead_dense), None);
}

/// Dual-review defect D1 (Critical): the pre-fix guard was an
/// allowlist-of-one (`== IndexBudgetExceeded`) where the story's own
/// docs demand an allowlist of exactly ONE good state (`!= Complete`
/// suppresses everything else). `RepoIndexIncomplete` (repo-level
/// indexing gaps: `max_files` truncation, parse errors, extractor
/// panics, unreadable source files -- see `repo_index::build_repo_graph`)
/// is the DISCRIMINATING case a wrong `== IndexBudgetExceeded` guard
/// would miss: it is non-`Complete` but not `IndexBudgetExceeded`,
/// so the old guard fell through to `Some(true)` -- a confident
/// "definitely dead" verdict from a partially-indexed repository.
#[test]
fn is_definitely_dead_code_suppresses_the_dead_code_tier_for_every_non_complete_state() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let dead_symbol = builder.intern_symbol(make_symbol_id(1, 0));
    builder.set_completeness(crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete);
    let graph = builder.build();

    assert_eq!(
        graph.is_definitely_dead_code(dead_symbol),
        None,
        "an unreferenced symbol in a RepoIndexIncomplete graph must be suppressed (None), \
         never a confident Some(true) 'definitely dead' verdict"
    );
}

/// `downgrade_completeness` must set the reason exactly once (from the
/// default `Complete`) and never clobber an already-recorded, more
/// specific reason with a later, less specific one.
#[test]
fn downgrade_completeness_sets_reason_once_but_never_clobbers_an_existing_one() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    builder.intern_symbol(make_symbol_id(1, 0));
    let mut graph = builder.build();
    assert_eq!(graph.completeness(), crate::graph::budget::AnalysisCompleteness::Complete);

    graph.downgrade_completeness(crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete);
    assert_eq!(graph.completeness(), crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete);

    // A second, different reason must NOT overwrite the first.
    graph.downgrade_completeness(crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);
    assert_eq!(
        graph.completeness(),
        crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete,
        "the first-recorded degradation reason must win"
    );
}

/// Defect 2 (ADR-002 GraphHandle FFI fix): `try_resolve_symbol`/
/// `try_resolve_string` are the checked counterparts to
/// `resolve_symbol`/`resolve_string` -- they must delegate to
/// `SymbolTable::try_resolve`/`StringTable::try_resolve` and return
/// `None` on an out-of-range id, never panic. The panicking
/// `resolve_symbol`/`resolve_string` are unchanged and still covered by
/// `graph_round_trips_references_candidates_symbols_and_strings` above.
#[test]
fn try_resolve_symbol_and_try_resolve_string_are_checked_never_panicking_counterparts() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let foo_symbol = make_symbol_id(1, 0);
    let foo_dense = builder.intern_symbol(foo_symbol);
    let foo_name_id = builder.intern_string("Foo");
    let graph = builder.build();

    assert_eq!(graph.try_resolve_symbol(foo_dense), Some(foo_symbol));
    assert_eq!(graph.try_resolve_symbol(u32::MAX), None, "an out-of-range dense id must return None, never panic");

    assert_eq!(graph.try_resolve_string(foo_name_id), Some("Foo"));
    assert_eq!(graph.try_resolve_string(u32::MAX), None, "an out-of-range string id must return None, never panic");
}

/// Bug #1833 AC1 (discriminating regression test -- MUST fail on
/// unmodified code, not just on a contrived input): a `Complete` graph
/// carries NO visibility or entry-point data anywhere in the CSR arena
/// (`SymbolId`, `Candidate`, `Reference`, `Declaration` all lack any
/// modifier field -- verified by inspection before writing this test).
/// So an unreferenced symbol here is exactly the shape of a library's
/// public API symbol on a real repo: zero in-repo callers BY
/// CONSTRUCTION, not because it is provably unreachable. Reporting
/// `Some(true)` ("definitely dead") for it is a false certainty the
/// graph cannot back up -- confirmed live on jsoup-global (Bug #1833:
/// 1296/3147 symbols wrongly flagged, including documented public API
/// like `Connection.contentType`). A test using a symbol some OTHER
/// signal proves private would pass today on the pre-fix code too and
/// prove nothing; this one does not smuggle in any such signal.
#[test]
fn is_definitely_dead_code_does_not_claim_certainty_for_an_unreferenced_symbol_with_no_visibility_evidence() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let library_api_symbol = builder.intern_symbol(make_symbol_id(1, 0));
    // Completeness defaults to `Complete` -- the exact condition under
    // which the pre-fix code fell through to `Some(true)`.
    let graph = builder.build();

    assert_eq!(
        graph.is_definitely_dead_code(library_api_symbol),
        None,
        "an unreferenced symbol on a Complete graph must be reported as undecidable (None) \
         when the graph holds no evidence the symbol is unreachable from OUTSIDE the repo -- \
         claiming Some(true) here is exactly Bug #1833's false 'definitely dead' verdict"
    );
}

/// Story #1835 AC4 (RED against unmodified code -- `CodeGraphBuilder`
/// has no `add_visibility` method yet, so this fails to compile): the
/// CENTRAL discriminating test for the whole story. On a SINGLE
/// `Complete` graph, an unreferenced PRIVATE symbol must yield
/// `Some(true)` (a real, provable dead-code verdict) while an
/// unreferenced PUBLIC symbol in that SAME graph must stay `None`
/// (Bug #1833's conservative behavior, unchanged for anything the
/// visibility bit cannot prove safe). A test exercising only one of
/// the two directions would prove nothing -- the whole point of this
/// story is that the function now tells them apart.
#[test]
fn is_definitely_dead_code_distinguishes_unreferenced_private_from_unreferenced_public_on_a_complete_graph() {
    use crate::graph::extract::local_index::{DeclarationKind, Visibility};

    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let unreferenced_private = builder.intern_symbol(make_symbol_id(1, 0));
    let unreferenced_public = builder.intern_symbol(make_symbol_id(1, 1));
    builder.add_visibility(unreferenced_private, Visibility::Private);
    builder.add_visibility(unreferenced_public, Visibility::Public);
    // Bug #1858: both symbols here represent methods, so they carry a
    // tracked-reference kind -- real production code always attaches
    // one (see `budget_bind::intern_declarations_and_attach_signatures`),
    // and without it `is_definitely_dead_code` now correctly stays
    // undecidable regardless of visibility.
    builder.add_kind(unreferenced_private, DeclarationKind::Method);
    builder.add_kind(unreferenced_public, DeclarationKind::Method);
    // Completeness defaults to `Complete`.
    let graph = builder.build();

    assert_eq!(
        graph.is_definitely_dead_code(unreferenced_private),
        Some(true),
        "an unreferenced PRIVATE symbol is provably unreachable from outside the repo -- \
         this is the true positive Bug #1833's fix gave up and this story restores"
    );
    assert_eq!(
        graph.is_definitely_dead_code(unreferenced_public),
        None,
        "an unreferenced PUBLIC symbol stays undecidable -- external callers are invisible \
         to this repo's graph by construction, exactly Bug #1833's jsoup Connection/Response case"
    );
}

/// Bug #1858: field reads are not represented by inbound reference edges,
/// so an unreferenced private field must not be classified as definitely
/// dead. Keep the unreferenced private method in the same test so this
/// remains discriminating: tracked declaration kinds still get the dead
/// verdict. Turn 7 (codex) proved this RED against unmodified code using
/// only `add_visibility` -- `add_kind` did not exist yet. Turn 8 (claude)
/// extends it in place with real `add_kind` calls now that the
/// declaration-kind channel exists; this must STILL be red at this point
/// (`is_definitely_dead_code` does not consult kind yet -- that is turn
/// 9's job), for the identical reason as before.
#[test]
fn is_definitely_dead_code_does_not_claim_unreferenced_private_field_is_dead() {
    use crate::graph::extract::local_index::{DeclarationKind, Visibility};

    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    // Intended field: its source-level read cannot become an inbound edge
    // with the current extractor, so the graph sees no reference here.
    let private_field = builder.intern_symbol(make_symbol_id(1, 0));
    // Intended method: genuinely unreferenced and tracked by the graph.
    let private_method = builder.intern_symbol(make_symbol_id(1, 1));
    builder.add_visibility(private_field, Visibility::Private);
    builder.add_visibility(private_method, Visibility::Private);
    builder.add_kind(private_field, DeclarationKind::Field);
    builder.add_kind(private_method, DeclarationKind::Method);
    let graph = builder.build();

    assert_eq!(graph.is_definitely_dead_code(private_field), None);
    assert_eq!(graph.is_definitely_dead_code(private_method), Some(true));
}

/// Bug #1858: `kind_for` must report back exactly the `DeclarationKind`
/// attached via `add_kind` on a normal (non-degraded) build -- the
/// baseline round trip every other `kind_for` test builds on.
#[test]
fn kind_for_returns_the_declared_kind_after_a_normal_build() {
    use crate::graph::extract::local_index::DeclarationKind;

    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let field_symbol = builder.intern_symbol(make_symbol_id(1, 0));
    let method_symbol = builder.intern_symbol(make_symbol_id(1, 1));
    builder.add_kind(field_symbol, DeclarationKind::Field);
    builder.add_kind(method_symbol, DeclarationKind::Method);
    let graph = builder.build();

    assert_eq!(graph.kind_for(field_symbol), Some(DeclarationKind::Field));
    assert_eq!(graph.kind_for(method_symbol), Some(DeclarationKind::Method));
}

/// Bug #1858 safe-default contract, half 1: a genuinely BUDGET-EXCEEDED
/// build must still retain declaration kinds, exactly like
/// `visibilities` (never dropped like `signatures`) -- otherwise a
/// degraded build would silently lose the evidence that keeps a
/// tracked-reference kind (e.g. Method) eligible for its existing
/// `Some(true)` verdict, an unrelated regression this bug must not
/// introduce. `set_completeness` here simulates the degraded state
/// directly on the builder, mirroring
/// `budget_outcome_fields_round_trip_through_the_builder` above --
/// `kinds` has no separate "drop under budget" code path to simulate
/// (unlike `signatures`, which `bind_with_budget` explicitly skips
/// writing), so retention is verified as a direct, unconditional
/// consequence of `add_kind` always being called.
#[test]
fn kind_for_is_retained_under_a_simulated_budget_exceeded_build() {
    use crate::graph::extract::local_index::DeclarationKind;

    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let method_symbol = builder.intern_symbol(make_symbol_id(1, 0));
    builder.add_kind(method_symbol, DeclarationKind::Method);
    builder.set_completeness(crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);
    let graph = builder.build();

    assert_eq!(
        graph.completeness(),
        crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded
    );
    assert_eq!(
        graph.kind_for(method_symbol),
        Some(DeclarationKind::Method),
        "declaration kind must survive a budget-exceeded build, mirroring visibility \
         retention, so a degraded build never loses the evidence a tracked-reference kind \
         needs to keep its existing dead-code verdict"
    );
}

/// Bug #1858 safe-default contract, half 2: a symbol with NO kind
/// evidence at all (never passed to `add_kind`) must read back as
/// `None`, never fabricate a kind. This is the entry-absent case,
/// distinct from the budget-exceeded-but-present case above --
/// `is_definitely_dead_code` must treat this exactly as "unproven",
/// never as license to report `Some(true)`.
#[test]
fn kind_for_returns_none_for_a_symbol_with_no_kind_evidence() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let unknown_kind_symbol = builder.intern_symbol(make_symbol_id(1, 0));
    let graph = builder.build();

    assert_eq!(
        graph.kind_for(unknown_kind_symbol),
        None,
        "a symbol never passed to add_kind must read back as None, never a fabricated kind"
    );
}

/// Bug #1833 AC2/AC3/AC4: on a library-shaped graph, raw reference
/// evidence remains queryable independently of the conservative
/// definitely-dead verdict. The referenced symbol is still known live,
/// while both unreferenced symbols are undecidable rather than falsely
/// classified as dead. This makes `definitely_dead < unreferenced`
/// structural for the current visibility-blind graph representation.
#[test]
fn library_graph_under_reports_dead_code_without_aliasing_raw_references() {
    let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
    let referenced_symbol = builder.intern_symbol(make_symbol_id(1, 0));
    let unreferenced_api_a = builder.intern_symbol(make_symbol_id(1, 1));
    let unreferenced_api_b = builder.intern_symbol(make_symbol_id(1, 2));
    builder.mark_referenced(referenced_symbol);

    let graph = builder.build();

    let unreferenced = (0..graph.symbol_count() as u32)
        .filter(|&dense_id| !graph.is_symbol_referenced(dense_id))
        .count();
    let definitely_dead = (0..graph.symbol_count() as u32)
        .filter(|&dense_id| graph.is_definitely_dead_code(dense_id) == Some(true))
        .count();

    assert_eq!(unreferenced, 2);
    assert_eq!(definitely_dead, 0);
    assert!(definitely_dead < unreferenced);

    // AC3: the public raw query still reports the actual reference bit.
    assert!(graph.is_symbol_referenced(referenced_symbol));
    assert!(!graph.is_symbol_referenced(unreferenced_api_a));
    assert!(!graph.is_symbol_referenced(unreferenced_api_b));
    // AC4: positive in-repo evidence remains Some(false).
    assert_eq!(graph.is_definitely_dead_code(referenced_symbol), Some(false));
    // The two queries are deliberately not synonyms for unreferenced API.
    assert_eq!(graph.is_definitely_dead_code(unreferenced_api_a), None);
    assert_eq!(graph.is_definitely_dead_code(unreferenced_api_b), None);
}
