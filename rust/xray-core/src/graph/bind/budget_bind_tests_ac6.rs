//! `budget_bind.rs`'s relocated unit tests, second half (Messi Rule 6,
//! anti-file-bloat; mirrors `resolve.rs`'s own established `mod tests;`/
//! `mod tests_family;` multi-file test split). This half covers the AC6
//! budget-ladder tests -- see `budget_bind_tests.rs` for the first half
//! (`widen_method_signature`/varargs) and its shared `method_decl`/
//! `invocation`/`file` fixture helpers, reached here the same way
//! `resolve_tests_family.rs` reaches `resolve_tests.rs`'s.

use super::*;
use crate::graph::budget::AnalysisCompleteness;
use crate::graph::extract::local_index::{Declaration, DeclarationKind, LocalIndex};
use crate::graph::identity::{make_symbol_id, SymbolId};
use super::tests::{file, invocation, method_decl};

/// Three same-named `run` declarations, none sharing a file (or
/// package/import) with the call site: zero distinguishing evidence,
/// so all three stay equally NameOnly-confidence and genuinely
/// ambiguous -- nothing narrows the set before the AC6 cap. Plus a
/// unique, non-ambiguous declaration invoked separately, to push the
/// repo-wide RAW candidate total (4) past a tight ceiling (3).
fn ambiguous_run_fixture() -> (Vec<FileForBind>, IndexBudget, Vec<SymbolId>) {
    let mut file_a = LocalIndex::new();
    file_a.declarations.push(method_decl("run", 1, 0, None));
    let mut file_b = LocalIndex::new();
    file_b.declarations.push(method_decl("run", 2, 0, None));
    let mut file_c = LocalIndex::new();
    file_c.declarations.push(method_decl("run", 3, 0, None));
    let mut caller = LocalIndex::new();
    caller.invocations.push(invocation("run", None));
    caller.invocations.push(invocation("uniqueOne", Some(0)));
    let mut file_unique = LocalIndex::new();
    file_unique
        .declarations
        .push(method_decl("uniqueOne", 5, 0, None));

    let files = vec![
        file(1, "java", file_a),
        file(2, "java", file_b),
        file(3, "java", file_c),
        file(4, "java", caller),
        file(5, "java", file_unique),
    ];
    let run_symbols: Vec<SymbolId> = [1u32, 2u32, 3u32]
        .iter()
        .map(|&f| make_symbol_id(f, 0))
        .collect();
    (files, IndexBudget::new(3, 1), run_symbols)
}

/// AC6's central discriminating test (named explicitly in the story):
/// a symbol referenced ONLY by a LOW-CONFIDENCE, ambiguous edge must
/// still be reported as referenced after the budget forces a
/// per-reference top-N cap that truncates that exact candidate out of
/// the CSR arena. A wrong implementation that marked `ReferencedBits`
/// from the CAPPED (post-truncation) candidate list -- rather than the
/// raw, pre-cap list -- would report the truncated-away symbols as
/// unreferenced: the false "this code is dead" verdict AC6 exists to
/// prevent.
#[test]
fn symbol_referenced_only_by_a_low_confidence_edge_still_reports_referenced_under_budget() {
    let (files, budget, run_symbols) = ambiguous_run_fixture();
    let graph = bind_with_budget(files, &budget);

    assert_eq!(
        graph.completeness(),
        AnalysisCompleteness::IndexBudgetExceeded
    );

    let run_reference = graph
        .references()
        .iter()
        .find(|r| {
            r.kind == super::super::REF_KIND_INVOCATION
                && graph
                    .candidates_for(r)
                    .iter()
                    .any(|c| run_symbols.contains(&graph.resolve_symbol(c.symbol())))
        })
        .expect("expected to find the 'run' reference by one of its surviving candidates");
    assert_eq!(
        graph.candidates_for(run_reference).len(),
        1,
        "cap must narrow the 3-way ambiguous set to 1"
    );

    // All three "run" declarations are NameOnly confidence (no
    // distinguishing evidence whatsoever) -- every one of them,
    // including the two capped away, must still report referenced.
    for &symbol in &run_symbols {
        let dense = graph
            .dense_id_for(symbol)
            .expect("declared symbol must be interned");
        assert!(
            graph.is_symbol_referenced(dense),
            "symbol {symbol:#x} lost its referenced bit after cap truncation"
        );
        assert_eq!(
            graph.is_definitely_dead_code(dense),
            Some(false),
            "a referenced symbol must never be reported as (even possibly) dead"
        );
    }
}

/// Story #1835 AC2 (RED against unmodified code --
/// `intern_declarations_and_attach_signatures` never calls
/// `add_visibility`, so the `LocalIndex.visibilities` entry set here
/// never reaches the built graph and this stays `None`): proves
/// visibility survives the FULL extraction-to-CSR pipeline, not just
/// the isolated `CodeGraphBuilder` unit tested in `code_graph.rs`.
#[test]
fn unreferenced_private_declaration_reports_definitely_dead_after_binding_end_to_end() {
    use crate::graph::extract::local_index::Visibility;

    let mut index = LocalIndex::new();
    let symbol = make_symbol_id(1, 0);
    index.declarations.push(method_decl("hidden", 1, 0, None));
    index.visibilities.insert(symbol, Visibility::Private);

    let files = vec![file(1, "java", index)];
    let graph = bind_with_budget(files, &IndexBudget::unlimited());

    let dense = graph
        .dense_id_for(symbol)
        .expect("declared symbol must be interned");
    assert!(
        !graph.is_symbol_referenced(dense),
        "fixture sanity: hidden() must have zero callers"
    );
    assert_eq!(
        graph.is_definitely_dead_code(dense),
        Some(true),
        "an unreferenced PRIVATE declaration's visibility must survive end-to-end from \
         LocalIndex.visibilities through the builder into a real dead-code verdict"
    );
}

fn field_decl(name: &str, file_id: u32, local: u32) -> Declaration {
    Declaration {
        kind: DeclarationKind::Field,
        name: name.to_string(),
        line: 1,
        symbol: make_symbol_id(file_id, local),
        param_count: None,
        param_types: Vec::new(),
        is_varargs: false,
        vararg_index: None,
    }
}

/// Bug #1858: proves `DeclarationKind` survives the FULL
/// extraction-to-CSR pipeline (not just the isolated
/// `CodeGraphBuilder` unit tests in `code_graph.rs`), on BOTH a normal
/// and a genuinely budget-exceeded real `bind_with_budget` call --
/// mirroring `unreferenced_private_declaration_reports_definitely_dead_after_binding_end_to_end`
/// (visibility's end-to-end proof) and reusing `dup_pair`'s
/// budget-overage shape from
/// `signatures_are_present_when_budget_is_not_exceeded_and_dropped_when_it_is`.
/// Unlike that signature test, kind must NOT be dropped under budget
/// pressure -- it must behave like visibility, not like signatures.
/// Split into `unlimited`/`exceeded` halves to stay under the
/// per-function length guideline.
fn declaration_kind_survives_an_unlimited_bind() -> (SymbolId, SymbolId) {
    let mut index = LocalIndex::new();
    let field_symbol = make_symbol_id(1, 0);
    let method_symbol = make_symbol_id(1, 1);
    index.declarations.push(field_decl("count", 1, 0));
    index.declarations.push(method_decl("hidden", 1, 1, None));

    let unlimited_graph =
        bind_with_budget(vec![file(1, "java", index)], &IndexBudget::unlimited());
    assert_eq!(
        unlimited_graph.completeness(),
        AnalysisCompleteness::Complete
    );
    let field_dense = unlimited_graph
        .dense_id_for(field_symbol)
        .expect("field must be interned");
    let method_dense = unlimited_graph
        .dense_id_for(method_symbol)
        .expect("method must be interned");
    assert_eq!(
        unlimited_graph.kind_for(field_dense),
        Some(DeclarationKind::Field),
        "a Field declaration's kind must survive end-to-end from LocalIndex.declarations \
         through the builder into a real CodeGraph"
    );
    assert_eq!(
        unlimited_graph.kind_for(method_dense),
        Some(DeclarationKind::Method)
    );
    (field_symbol, method_symbol)
}

#[test]
fn declaration_kind_is_retained_end_to_end_through_the_real_extraction_to_csr_pipeline_regardless_of_budget_pressure(
) {
    let (field_symbol, _method_symbol) = declaration_kind_survives_an_unlimited_bind();

    let mut index_for_exceeded = LocalIndex::new();
    index_for_exceeded
        .declarations
        .push(field_decl("count", 1, 0));
    let (dup_a, dup_b) = dup_pair();
    let exceeded_graph = bind_with_budget(
        vec![
            file(1, "java", index_for_exceeded),
            file(2, "java", dup_a),
            file(3, "java", dup_b),
        ],
        &IndexBudget::new(0, 5),
    );
    assert_eq!(
        exceeded_graph.completeness(),
        AnalysisCompleteness::IndexBudgetExceeded
    );
    let field_dense_exceeded = exceeded_graph
        .dense_id_for(field_symbol)
        .expect("field must be interned");
    assert_eq!(
        exceeded_graph.kind_for(field_dense_exceeded),
        Some(DeclarationKind::Field),
        "declaration kind must survive a genuinely budget-exceeded real bind, mirroring \
         visibility retention -- it must never be dropped like signatures are"
    );
}

fn solo_declared_symbol_with_signature() -> LocalIndex {
    let mut index = LocalIndex::new();
    index.declarations.push(method_decl("solo", 1, 0, None));
    index
        .signatures
        .insert(make_symbol_id(1, 0), "solo()".to_string());
    index
}

fn dup_pair() -> (LocalIndex, LocalIndex) {
    let mut dup_a = LocalIndex::new();
    dup_a.declarations.push(method_decl("dup", 2, 0, None));
    let mut dup_b = LocalIndex::new();
    dup_b.declarations.push(method_decl("dup", 3, 0, None));
    dup_b.invocations.push(invocation("dup", None));
    (dup_a, dup_b)
}

/// AC6 ladder step 1 ("drop snippets first"): unconditional on ANY
/// repo-wide overage, independent of whether THIS particular symbol's
/// own reference ever needed capping -- demonstrated here with an
/// unrelated, unambiguous "dup" pair that alone pushes the raw total
/// over budget while "solo" itself is never even referenced.
#[test]
fn signatures_are_present_when_budget_is_not_exceeded_and_dropped_when_it_is() {
    let unlimited_graph = bind_with_budget(
        vec![file(1, "java", solo_declared_symbol_with_signature())],
        &IndexBudget::unlimited(),
    );
    let dense = unlimited_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
    // Bug #1904: `method_decl`'s fixture shape uses `param_count: None`
    // (arity untracked in this minimal test helper -- a real extractor
    // always sets `Some`, see `widen_method_signature`'s doc comment),
    // so `param_types.len() (0) != param_count.unwrap_or(usize::MAX)`
    // and the widening falls back to the arity-only form; no
    // `MethodOwnerRecord` is set here either, so there is no declaring-
    // type prefix. This test's own purpose (presence-vs-dropped under
    // budget pressure) is unaffected by the CONTENT of the string.
    assert_eq!(unlimited_graph.signature_for(dense), Some("solo(0 params)"));
    assert_eq!(
        unlimited_graph.completeness(),
        AnalysisCompleteness::Complete
    );

    let (dup_a, dup_b) = dup_pair();
    let exceeded_graph = bind_with_budget(
        vec![
            file(1, "java", solo_declared_symbol_with_signature()),
            file(2, "java", dup_a),
            file(3, "java", dup_b),
        ],
        &IndexBudget::new(0, 5),
    );
    assert_eq!(
        exceeded_graph.completeness(),
        AnalysisCompleteness::IndexBudgetExceeded
    );
    let dense = exceeded_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
    assert_eq!(
        exceeded_graph.signature_for(dense),
        None,
        "snippets must be dropped once the budget is exceeded, even for an unrelated symbol"
    );
}

/// Bug #1833: the "no reference at all" finding tier is suppressed
/// (`None`) for an unreferenced symbol REGARDLESS of completeness --
/// including on a `Complete` build. Before the fix this test asserted
/// `Some(true)` for the `Complete` case, which was exactly the false
/// certainty Bug #1833 reported live on jsoup-global: a `Complete`
/// graph proves every in-repo file parsed cleanly, not that no
/// external caller exists, so an unreferenced method here is
/// indistinguishable from a library's unreferenced-in-repo public API.
#[test]
fn strongest_dead_code_tier_is_suppressed_regardless_of_completeness() {
    fn never_called_file() -> LocalIndex {
        let mut index = LocalIndex::new();
        index
            .declarations
            .push(method_decl("neverCalled", 1, 0, None));
        index
    }

    let complete_graph = bind_with_budget(
        vec![file(1, "java", never_called_file())],
        &IndexBudget::unlimited(),
    );
    let dense = complete_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
    assert_eq!(
        complete_graph.is_definitely_dead_code(dense),
        None,
        "an unreferenced symbol on a Complete graph must be undecidable (None), never a \
         confident Some(true) -- see Bug #1833"
    );

    let (dup_a, dup_b) = dup_pair();
    let exceeded_graph = bind_with_budget(
        vec![
            file(1, "java", never_called_file()),
            file(2, "java", dup_a),
            file(3, "java", dup_b),
        ],
        &IndexBudget::new(0, 5),
    );
    let dense = exceeded_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
    assert_eq!(
        exceeded_graph.is_definitely_dead_code(dense),
        None,
        "the 'no reference at all' tier must also be suppressed under IndexBudgetExceeded"
    );
}

/// `AnalysisCompleteness` must report the graph's REAL state, not
/// always `Complete` -- `Complete` for an unlimited budget, and the
/// SPECIFIC `IndexBudgetExceeded` variant once the ladder engages.
#[test]
fn completeness_reports_the_real_build_state() {
    let (dup_a, dup_b) = dup_pair();
    let complete = bind_with_budget(
        vec![file(2, "java", dup_a), file(3, "java", dup_b)],
        &IndexBudget::unlimited(),
    );
    assert_eq!(complete.completeness(), AnalysisCompleteness::Complete);

    let (dup_a, dup_b) = dup_pair();
    let exceeded = bind_with_budget(
        vec![file(2, "java", dup_a), file(3, "java", dup_b)],
        &IndexBudget::new(0, 5),
    );
    assert_eq!(
        exceeded.completeness(),
        AnalysisCompleteness::IndexBudgetExceeded
    );
}

/// Dual-review defect D3: `bind_with_budget_and_completeness` is the
/// real public entry point a caller with partial-index knowledge
/// (`repo_index::build_repo_graph`) must use. `index_is_complete =
/// false` must disable `UNIQUE_NAME_IN_REPO`/`Confidence::Exact`
/// end-to-end through the public API, even though `bind_with_budget`
/// (the `index_is_complete = true` convenience wrapper) would still
/// grant it for the identical input.
#[test]
fn bind_with_budget_and_completeness_disables_unique_name_shortcut_when_index_is_partial() {
    use crate::graph::confidence::Confidence;

    let files = || {
        let mut solo = LocalIndex::new();
        solo.declarations.push(method_decl("onlyOne", 1, 0, None));
        let mut caller = LocalIndex::new();
        caller.invocations.push(invocation("onlyOne", None));
        vec![file(1, "java", solo), file(2, "java", caller)]
    };

    let complete_graph =
        bind_with_budget_and_completeness(files(), &IndexBudget::unlimited(), true);
    let complete_ref = complete_graph
        .references()
        .iter()
        .find(|r| !r.is_unresolved())
        .expect("the call must resolve to something");
    let complete_candidate = &complete_graph.candidates_for(complete_ref)[0];
    assert_eq!(complete_candidate.confidence(), Confidence::Exact);

    let partial_graph =
        bind_with_budget_and_completeness(files(), &IndexBudget::unlimited(), false);
    let partial_ref = partial_graph
        .references()
        .iter()
        .find(|r| !r.is_unresolved())
        .expect("the call must still resolve to the sole indexed declaration");
    let partial_candidate = &partial_graph.candidates_for(partial_ref)[0];
    assert_ne!(
        partial_candidate.confidence(),
        Confidence::Exact,
        "a partial index must never grant Exact confidence through the public bind API"
    );
}
