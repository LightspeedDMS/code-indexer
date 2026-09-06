//! AC6 budgeted binder entry point (Story #1787, S2): the fail-closed
//! degradation ladder wired against a real `bind`. Split out of
//! `super::mod` to keep that module under the project's per-module line
//! budget, mirroring how `resolve`/`name_index`/`scope`/`depth` each carry
//! their own focused logic and tests.
//!
//! See `crate::graph::budget` for the ladder's individual pieces
//! (`IndexBudget`, `ReferencedBits`, `cap_top_n_by_confidence`,
//! `AnalysisCompleteness`) and their own unit tests; this module is where
//! they are actually composed against a real multi-file bind.

use super::{FileForBind, PendingReference};
use crate::graph::budget::IndexBudget;
use crate::graph::csr::{CodeGraph, CodeGraphBuilder};

/// AC6: the exact final CSR candidate-arena size once the ladder's step-2
/// cap is (or is not) applied -- must be computed BEFORE
/// `CodeGraphBuilder::with_candidate_capacity`, which needs the exact
/// final count up front. Terminates in one pass over `pending` (finite:
/// one entry per reference `resolve_all_references` produced, itself
/// bounded by the finite invocation/type-reference/construction counts
/// extracted per file).
pub(super) fn capped_candidate_total(pending: &[PendingReference], exceeded: bool, max_per_reference: usize) -> usize {
    if !exceeded {
        return pending.iter().map(|r| r.candidates.len()).sum();
    }
    pending.iter().map(|r| r.candidates.len().min(max_per_reference)).sum()
}

/// Interns EVERY declared symbol across `files` -- not just symbols that
/// happen to appear as a reference's `from` or as some candidate's target
/// -- and, unless `exceeded` (AC6 ladder step 1, "drop snippets first"),
/// attaches its cached AC2 signature line too. Interning every declared
/// symbol MUST run unconditionally regardless of budget pressure: a
/// symbol nobody ever calls (the exact case `is_definitely_dead_code`
/// exists to report on) would otherwise never be interned at all, making
/// it unqueryable rather than correctly "unreferenced".
pub(super) fn intern_declarations_and_attach_signatures(files: &[FileForBind], builder: &mut CodeGraphBuilder, exceeded: bool) {
    for file in files {
        for declaration in &file.index.declarations {
            let dense = builder.intern_symbol(declaration.symbol);
            if exceeded {
                continue;
            }
            if let Some(signature) = file.index.signatures.get(&declaration.symbol) {
                builder.add_signature(dense, signature.clone());
            }
        }
    }
}

/// AC6 budgeted binder entry point. `super::bind()` delegates here with
/// `IndexBudget::unlimited()` -- a budget that can never be exceeded, so
/// the ladder never engages and `completeness()` always reports
/// `Complete`, matching `bind()`'s pre-AC6 behavior byte-for-byte.
///
/// Ladder, applied only when `budget.is_exceeded_by(total raw
/// candidates)`: (1) `intern_declarations_and_attach_signatures` simply
/// skips `add_signature` -- the cheapest step, dropped first; (2)
/// `cap_top_n_by_confidence` narrows each reference's own candidate
/// window; (3) `builder.mark_referenced` is called, per candidate, from
/// the RAW list BEFORE step 2 ever truncates anything -- decoupled by
/// construction, not by a check; (4) completeness is set to
/// `IndexBudgetExceeded`, which is what makes
/// `CodeGraph::is_definitely_dead_code` suppress the strongest dead-code
/// tier.
pub fn bind_with_budget(files: Vec<FileForBind>, budget: &IndexBudget) -> CodeGraph {
    // Story #1787 AC12: re-expressed in terms of the two-step admission
    // split (`super::admission`) so there is exactly ONE copy of the
    // ladder logic. `_stats` is discarded here -- `bind_with_budget`
    // itself never gates on anything; `admission::bind_with_admission_gate`
    // is the entry point an external caller uses when it wants to.
    let (prepared, _stats) = super::admission::prepare_bind(files);
    super::admission::finish_bind(prepared, budget)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::budget::AnalysisCompleteness;
    use crate::graph::extract::local_index::{Declaration, DeclarationKind, InvocationSite, LocalIndex};
    use crate::graph::identity::{make_symbol_id, SymbolId};

    fn method_decl(name: &str, file_id: u32, local: u32, param_count: Option<usize>) -> Declaration {
        Declaration {
            kind: DeclarationKind::Method,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(file_id, local),
            param_count,
        }
    }

    fn invocation(name: &str, arg_count: Option<usize>) -> InvocationSite {
        InvocationSite { callee_name: name.to_string(), line: 10, arg_count }
    }

    fn file(file_id: u32, language: &str, index: LocalIndex) -> FileForBind {
        FileForBind { file_id, language: language.to_string(), index }
    }

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
        file_unique.declarations.push(method_decl("uniqueOne", 5, 0, None));

        let files = vec![
            file(1, "java", file_a),
            file(2, "java", file_b),
            file(3, "java", file_c),
            file(4, "java", caller),
            file(5, "java", file_unique),
        ];
        let run_symbols: Vec<SymbolId> = [1u32, 2u32, 3u32].iter().map(|&f| make_symbol_id(f, 0)).collect();
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

        assert_eq!(graph.completeness(), AnalysisCompleteness::IndexBudgetExceeded);

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
        assert_eq!(graph.candidates_for(run_reference).len(), 1, "cap must narrow the 3-way ambiguous set to 1");

        // All three "run" declarations are NameOnly confidence (no
        // distinguishing evidence whatsoever) -- every one of them,
        // including the two capped away, must still report referenced.
        for &symbol in &run_symbols {
            let dense = graph.dense_id_for(symbol).expect("declared symbol must be interned");
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

    fn solo_declared_symbol_with_signature() -> LocalIndex {
        let mut index = LocalIndex::new();
        index.declarations.push(method_decl("solo", 1, 0, None));
        index.signatures.insert(make_symbol_id(1, 0), "solo()".to_string());
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
        let unlimited_graph =
            bind_with_budget(vec![file(1, "java", solo_declared_symbol_with_signature())], &IndexBudget::unlimited());
        let dense = unlimited_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        assert_eq!(unlimited_graph.signature_for(dense), Some("solo()"));
        assert_eq!(unlimited_graph.completeness(), AnalysisCompleteness::Complete);

        let (dup_a, dup_b) = dup_pair();
        let exceeded_graph = bind_with_budget(
            vec![
                file(1, "java", solo_declared_symbol_with_signature()),
                file(2, "java", dup_a),
                file(3, "java", dup_b),
            ],
            &IndexBudget::new(0, 5),
        );
        assert_eq!(exceeded_graph.completeness(), AnalysisCompleteness::IndexBudgetExceeded);
        let dense = exceeded_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        assert_eq!(
            exceeded_graph.signature_for(dense),
            None,
            "snippets must be dropped once the budget is exceeded, even for an unrelated symbol"
        );
    }

    /// AC6: "the 'no reference at all' finding tier is SUPPRESSED under
    /// IndexBudgetExceeded". A genuinely unreferenced symbol reports
    /// `Some(true)` (definitely dead) on a `Complete` build, but `None`
    /// (suppressed) once the SAME kind of build is `IndexBudgetExceeded`.
    #[test]
    fn strongest_dead_code_tier_is_suppressed_under_index_budget_exceeded_but_not_when_complete() {
        fn never_called_file() -> LocalIndex {
            let mut index = LocalIndex::new();
            index.declarations.push(method_decl("neverCalled", 1, 0, None));
            index
        }

        let complete_graph = bind_with_budget(vec![file(1, "java", never_called_file())], &IndexBudget::unlimited());
        let dense = complete_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        assert_eq!(complete_graph.is_definitely_dead_code(dense), Some(true));

        let (dup_a, dup_b) = dup_pair();
        let exceeded_graph = bind_with_budget(
            vec![file(1, "java", never_called_file()), file(2, "java", dup_a), file(3, "java", dup_b)],
            &IndexBudget::new(0, 5),
        );
        let dense = exceeded_graph.dense_id_for(make_symbol_id(1, 0)).unwrap();
        assert_eq!(
            exceeded_graph.is_definitely_dead_code(dense),
            None,
            "the 'no reference at all' tier must be suppressed under IndexBudgetExceeded"
        );
    }

    /// `AnalysisCompleteness` must report the graph's REAL state, not
    /// always `Complete` -- `Complete` for an unlimited budget, and the
    /// SPECIFIC `IndexBudgetExceeded` variant once the ladder engages.
    #[test]
    fn completeness_reports_the_real_build_state() {
        let (dup_a, dup_b) = dup_pair();
        let complete =
            bind_with_budget(vec![file(2, "java", dup_a), file(3, "java", dup_b)], &IndexBudget::unlimited());
        assert_eq!(complete.completeness(), AnalysisCompleteness::Complete);

        let (dup_a, dup_b) = dup_pair();
        let exceeded = bind_with_budget(vec![file(2, "java", dup_a), file(3, "java", dup_b)], &IndexBudget::new(0, 5));
        assert_eq!(exceeded.completeness(), AnalysisCompleteness::IndexBudgetExceeded);
    }
}
