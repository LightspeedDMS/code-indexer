//! Story #1787 AC12 (amendment): Gate-2 structural separability.
//!
//! `bind_with_budget` (`super::budget_bind`) already computes its exact
//! candidate count via `resolve_all_references` BEFORE ever allocating the
//! CSR arena via `CodeGraphBuilder::with_candidate_capacity` -- that
//! sequencing was always there, just inline in one function. This module
//! promotes it into an explicit two-step public API so an external
//! admission gate (the server's real `MemoryGovernor`, Python-side -- see
//! `docs/adr/ADR-003-graph-memory-governor-integration.md`) can inspect
//! exact declaration/call-site/candidate-edge counts and refuse the build
//! BEFORE the expensive allocation ever runs, not merely discard its
//! result afterward.
//!
//! A follow-up change re-expresses `bind_with_budget` as
//! `finish_bind(prepare_bind(files).0, budget)` so there is exactly one
//! copy of the ladder logic (Rule 4, anti-duplication); until then this
//! module's `finish_bind` is a parallel expression of the same steps,
//! proven identical by `bind_with_budget_matches_prepare_then_finish_bind`
//! below.

use super::budget_bind::{capped_candidate_total, intern_declarations_and_attach_signatures};
use super::depth::{BinderDepth, LEVEL_0_BARE_NAME};
use super::name_index::RepoNameIndex;
use super::{mark_depth_for_reasons, resolve_all_references, FileForBind, PendingReference};
use crate::graph::budget::{ladder::cap_top_n_by_confidence, AnalysisCompleteness, IndexBudget};
use crate::graph::csr::{Candidate, CodeGraph, CodeGraphBuilder};
use std::collections::HashMap;

/// Exact counts computable BEFORE any CSR/candidate-arena allocation --
/// the numbers an external admission gate (AC12 Gate 2) needs to decide
/// whether the not-yet-allocated bound graph will fit in budget.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PreBindStats {
    pub declaration_count: usize,
    pub call_site_count: usize,
    pub candidate_edge_count: usize,
    /// #1898 round 4 (epic #1906, mandate item 3): how many references
    /// had a non-empty bare-name pool (a real same-named declaration
    /// exists somewhere in the repo) but were narrowed all the way down
    /// to ZERO final candidates -- see `super::resolve_all_references`'s
    /// own doc comment for the full rationale and how this is
    /// distinguished from an ordinary out-of-repo reference.
    pub narrowed_to_zero_count: usize,
    /// #1910 round 6 (finding 5, the mis-narrow counter `narrowed_to_zero_
    /// count` is blind to): how many references had a non-empty bare-name
    /// pool but were narrowed to a non-empty STRICT SUBSET of it -- see
    /// `super::resolve_all_references`'s own doc comment.
    pub narrowed_to_nonempty_strict_subset_count: usize,
}

/// Bug #1897 P1 fix: the two INDEPENDENT bind-time facts `finish_bind`
/// itself observes while building the graph, returned LOSSLESSLY
/// alongside the built `CodeGraph`.
///
/// `CodeGraph::set_completeness` (called once by `finish_bind`, below)
/// collapses these two booleans into a SINGLE first-write-wins
/// `AnalysisCompleteness` value -- that collapsed enum is exactly right
/// for `CodeGraph::is_definitely_dead_code`'s own internal suppression
/// (which only ever needs "is this graph anything other than `Complete`"),
/// so `CodeGraph`'s own single-slot completeness is left UNCHANGED by this
/// fix. But that same collapsed value is NOT a lossless record of what
/// actually happened during THIS bind: when `exceeded` and
/// `family_truncated` are both true, the old `if exceeded { .. } else if
/// family_truncated { .. }` chain (still below) reports only
/// `IndexBudgetExceeded`, silently discarding the fact that the family cap
/// also truncated. `repo_index::build_repo_graph` -- the one caller that
/// promises a LOSSLESS `completeness_reasons` list (#1897's own
/// requirement) -- reads these two booleans directly instead, never
/// reconstructing them from `graph.completeness()` after the collapse has
/// already happened.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct BindTimeFacts {
    pub index_budget_exceeded: bool,
    pub family_truncated: bool,
}

/// Everything `finish_bind` needs to complete the build. Deliberately
/// holds no `CodeGraphBuilder`/`CodeGraph` field -- by construction, no
/// CSR arena has been allocated by the time a `PreparedBind` exists.
pub struct PreparedBind {
    files: Vec<FileForBind>,
    depths: HashMap<String, BinderDepth>,
    pending: Vec<PendingReference>,
    total_candidates: usize,
    /// Memory-safety amendment: true when ANY reference's
    /// inheritance-family expansion was truncated by
    /// `families::MAX_FAMILY_SIZE` -- `finish_bind` uses this to report
    /// `AnalysisCompleteness::ResolutionAmbiguous`.
    family_truncated: bool,
}

/// AC12 Gate 2, step 1 ("measure"): resolves every reference across
/// `files` and returns exact stats, WITHOUT allocating the CSR candidate
/// arena. Bounded loop inherited from `resolve_all_references` (one pass
/// per file's finite invocation/type-reference/construction lists).
pub fn prepare_bind(
    files: Vec<FileForBind>,
    index_is_complete: bool,
) -> (PreparedBind, PreBindStats) {
    let name_index = RepoNameIndex::build(&files);
    let type_index = super::families::TypeIndex::build(&files);
    let mut depths: HashMap<String, BinderDepth> = HashMap::new();
    for file in &files {
        depths
            .entry(file.language.clone())
            .or_insert_with(|| BinderDepth::new(file.language.clone()));
    }
    let (
        pending,
        total_candidates,
        family_truncated,
        narrowed_to_zero_count,
        narrowed_to_nonempty_strict_subset_count,
    ) = resolve_all_references(&files, &name_index, &type_index, index_is_complete);
    let declaration_count = files.iter().map(|f| f.index.declarations.len()).sum();
    let call_site_count = pending.len();

    let stats = PreBindStats {
        declaration_count,
        call_site_count,
        candidate_edge_count: total_candidates,
        narrowed_to_zero_count,
        narrowed_to_nonempty_strict_subset_count,
    };
    let prepared = PreparedBind {
        files,
        depths,
        pending,
        total_candidates,
        family_truncated,
    };
    (prepared, stats)
}

/// AC12 Gate 2, step 2 ("allocate"): the exact remainder of the
/// pre-AC12 `bind_with_budget` body, starting at the ONE CSR-arena
/// allocation point (`CodeGraphBuilder::with_candidate_capacity`). Never
/// called by `bind_with_admission_gate` when the gate denies.
///
/// Bug #1897 P1 fix: returns `BindTimeFacts` alongside the built
/// `CodeGraph` -- the two independent booleans this function itself
/// computes (`exceeded`, `family_truncated`) BEFORE they are collapsed
/// into the single `AnalysisCompleteness` value `builder.set_completeness`
/// records. See `BindTimeFacts`'s own doc comment for why this second,
/// lossless channel exists.
pub fn finish_bind(
    prepared: PreparedBind,
    budget: &IndexBudget,
    file_paths: &HashMap<u32, String>,
) -> (CodeGraph, BindTimeFacts) {
    let PreparedBind {
        files,
        mut depths,
        pending,
        total_candidates,
        family_truncated,
    } = prepared;
    let exceeded = budget.is_exceeded_by(total_candidates);
    let max_per_reference = budget.max_candidates_per_reference();
    let capacity = capped_candidate_total(&pending, exceeded, max_per_reference);

    let mut builder = CodeGraphBuilder::with_candidate_capacity(capacity);
    intern_declarations_and_attach_signatures(&files, &mut builder, exceeded, file_paths);

    for reference in pending {
        let depth = depths
            .get_mut(&reference.language)
            .expect("language registered above");
        if !reference.candidates.is_empty() {
            depth.mark(LEVEL_0_BARE_NAME);
        }
        let from_dense = builder.intern_symbol(reference.from);
        let mut interned: Vec<(u32, u16)> = reference
            .candidates
            .iter()
            .map(|(decl, bits)| {
                mark_depth_for_reasons(depth, *bits);
                let dense = builder.intern_symbol(decl.symbol);
                builder.mark_referenced(dense);
                (dense, *bits)
            })
            .collect();
        if exceeded {
            cap_top_n_by_confidence(&mut interned, max_per_reference);
        }
        let built_candidates: Vec<Candidate> = interned
            .iter()
            .map(|(sym, bits)| Candidate::new(*sym, *bits))
            .collect();
        builder.add_reference(
            from_dense,
            reference.file,
            reference.line,
            reference.kind,
            &built_candidates,
        );
    }

    builder.set_binder_depths(depths.into_values().collect());
    builder.set_completeness(if exceeded {
        AnalysisCompleteness::IndexBudgetExceeded
    } else if family_truncated {
        // Memory-safety amendment: a family cap truncation is a
        // resolution-time concern, independent of (and reported even
        // when the budget ladder never engages -- see
        // `family_truncated_anywhere`'s doc comment on
        // `resolve_all_references`.
        AnalysisCompleteness::ResolutionAmbiguous
    } else {
        AnalysisCompleteness::Complete
    });
    let facts = BindTimeFacts { index_budget_exceeded: exceeded, family_truncated };
    (builder.build(), facts)
}

/// Outcome of a gated bind attempt (AC12 Gate 2). `Denied` carries the
/// exact `PreBindStats` an external admission decision was made against,
/// for observability -- never an empty/silent refusal (Rule 13).
pub enum BindOutcome {
    Denied(PreBindStats),
    Built(Box<CodeGraph>),
}

/// Composes `prepare_bind` and `finish_bind` around a caller-supplied
/// admission gate. THE central AC12 Gate-2 structural guarantee: `gate`
/// is consulted strictly BETWEEN measurement and allocation -- a `false`
/// return means `finish_bind` (and therefore
/// `CodeGraphBuilder::with_candidate_capacity`) is never invoked at all.
pub fn bind_with_admission_gate<F>(
    files: Vec<FileForBind>,
    budget: &IndexBudget,
    index_is_complete: bool,
    file_paths: &HashMap<u32, String>,
    gate: F,
) -> BindOutcome
where
    F: FnOnce(&PreBindStats) -> bool,
{
    let (prepared, stats) = prepare_bind(files, index_is_complete);
    if !gate(&stats) {
        return BindOutcome::Denied(stats);
    }
    // `BindTimeFacts` is discarded here -- `bind_with_admission_gate` has no
    // current caller that needs the lossless bind-time reasons (only
    // `repo_index::build_repo_graph` does today, and it calls `finish_bind`
    // directly, not through this gate). Add a `Built` facts field if a
    // future caller needs it; never re-derive from `graph.completeness()`.
    let (graph, _facts) = finish_bind(prepared, budget, file_paths);
    BindOutcome::Built(Box::new(graph))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::csr::builder::{
        candidate_capacity_allocation_count, reset_candidate_capacity_allocation_count,
    };
    use crate::graph::extract::local_index::{
        Declaration, DeclarationKind, InvocationSite, LocalIndex,
    };
    use crate::graph::identity::make_symbol_id;

    fn method_decl(
        name: &str,
        file_id: u32,
        local: u32,
        param_count: Option<usize>,
    ) -> Declaration {
        Declaration {
            kind: DeclarationKind::Method,
            name: name.to_string(),
            line: 1,
            symbol: make_symbol_id(file_id, local),
            param_count,
            param_types: Vec::new(),
            is_varargs: false,
            vararg_index: None,
        }
    }

    fn invocation(name: &str, arg_count: Option<usize>) -> InvocationSite {
        InvocationSite {
            callee_name: name.to_string(),
            line: 10,
            arg_count,
            arg_shapes: Vec::new(),
            receiver: crate::graph::extract::local_index::ReceiverExpr::None,
            enclosing_type: None,
            enclosing_method: None,
        }
    }

    fn file(file_id: u32, language: &str, index: LocalIndex) -> FileForBind {
        FileForBind {
            file_id,
            language: language.to_string(),
            index,
        }
    }

    fn two_file_fixture() -> Vec<FileForBind> {
        let mut a = LocalIndex::new();
        a.declarations.push(method_decl("run", 1, 0, None));
        a.invocations.push(invocation("run", None));
        let mut b = LocalIndex::new();
        b.declarations.push(method_decl("helper", 2, 0, None));
        b.invocations.push(invocation("helper", None));
        vec![file(1, "java", a), file(2, "java", b)]
    }

    /// AC12: `prepare_bind`'s stats must be EXACT -- 2 declarations, 2
    /// call sites, 2 resolved candidate edges (each invocation resolves
    /// to its own unique-name-in-repo declaration) -- and must match what
    /// a subsequent `finish_bind` actually produces.
    #[test]
    fn prepare_bind_reports_exact_counts_matching_a_subsequent_finish_bind() {
        let (prepared, stats) = prepare_bind(two_file_fixture(), true);
        assert_eq!(stats.declaration_count, 2);
        assert_eq!(stats.call_site_count, 2);
        assert_eq!(stats.candidate_edge_count, 2);

        let (graph, _facts) = finish_bind(prepared, &IndexBudget::unlimited(), &HashMap::new());
        let actual_candidates: usize = graph
            .references()
            .iter()
            .map(|r| graph.candidates_for(r).len())
            .sum();
        assert_eq!(
            actual_candidates, stats.candidate_edge_count,
            "PreBindStats must match the graph finish_bind actually produces"
        );
    }

    /// `bind_with_budget` (still unmodified in this change) must be
    /// byte-for-byte identical to `finish_bind(prepare_bind(files).0,
    /// budget)` -- proving the two-step split is a faithful decomposition
    /// before the follow-up change re-expresses `bind_with_budget` in
    /// terms of it.
    #[test]
    fn bind_with_budget_matches_prepare_then_finish_bind() {
        let via_public_api =
            super::super::bind_with_budget(two_file_fixture(), &IndexBudget::unlimited());
        let (prepared, _stats) = prepare_bind(two_file_fixture(), true);
        let (via_split_api, _facts) = finish_bind(prepared, &IndexBudget::unlimited(), &HashMap::new());

        assert_eq!(via_public_api.completeness(), via_split_api.completeness());
        assert_eq!(
            via_public_api.references().len(),
            via_split_api.references().len()
        );
    }

    /// THE central AC12 Gate-2 discriminating test: when `gate` denies,
    /// `bind_with_admission_gate` must NEVER invoke the ONE CSR-arena
    /// allocation point. A wrong implementation that built the full graph
    /// FIRST and checked the gate afterward (discarding the graph on
    /// denial) would still return `Denied` here but would have
    /// incremented the allocation counter -- exactly what this test
    /// exists to catch. The counter is thread-local (see
    /// `csr::builder`'s doc comment) so concurrently-running unrelated
    /// tests elsewhere in the crate cannot pollute this measurement.
    #[test]
    fn gate2_denial_never_allocates_the_candidate_arena() {
        reset_candidate_capacity_allocation_count();

        let outcome = bind_with_admission_gate(
            two_file_fixture(),
            &IndexBudget::unlimited(),
            true,
            &HashMap::new(),
            |_stats| false,
        );

        assert!(matches!(outcome, BindOutcome::Denied(_)));
        assert_eq!(
            candidate_capacity_allocation_count(),
            0,
            "the CSR candidate arena must NEVER be allocated when Gate 2 denies admission"
        );
    }

    /// Sanity companion: an ADMITTED build (gate returns true) DOES
    /// allocate exactly once, proving the counter itself is wired
    /// correctly and the denial test above isn't trivially passing
    /// because nothing ever increments it.
    #[test]
    fn gate2_admission_allocates_the_candidate_arena_exactly_once() {
        reset_candidate_capacity_allocation_count();

        let outcome = bind_with_admission_gate(
            two_file_fixture(),
            &IndexBudget::unlimited(),
            true,
            &HashMap::new(),
            |_stats| true,
        );

        assert!(matches!(outcome, BindOutcome::Built(_)));
        assert_eq!(candidate_capacity_allocation_count(), 1);
    }

    /// The gate receives the REAL stats (not a stub) -- proven by
    /// admitting only when the stats match the exact expected fixture
    /// counts.
    #[test]
    fn gate_receives_the_real_pre_bind_stats() {
        let outcome = bind_with_admission_gate(
            two_file_fixture(),
            &IndexBudget::unlimited(),
            true,
            &HashMap::new(),
            |stats| {
                stats.declaration_count == 2
                    && stats.call_site_count == 2
                    && stats.candidate_edge_count == 2
            },
        );
        assert!(
            matches!(outcome, BindOutcome::Built(_)),
            "gate must have observed the real, exact PreBindStats"
        );
    }

    /// Shared by `family_truncation_reports_resolution_ambiguous_
    /// completeness_even_under_an_unlimited_budget` and Bug #1897's own
    /// both-reasons discriminating test below: a common interface
    /// `Repo.save()` implemented by strictly more than `MAX_FAMILY_SIZE`
    /// classes, each in its OWN distinct package so import-context
    /// narrowing collapses to just the interface's own candidate BEFORE
    /// family expansion re-adds (and truncates past the cap) every
    /// implementor.
    fn family_truncation_fixture() -> Vec<FileForBind> {
        use super::super::families::MAX_FAMILY_SIZE;
        use crate::graph::extract::local_index::{
            InheritanceKind, InheritanceRecord, MethodOwnerRecord,
        };

        const INTERFACE_FILE_ID: u32 = 10;
        const CALLER_FILE_ID: u32 = 20;
        const IMPL_FILE_ID_BASE: u32 = 100;
        // Strictly more implementors than MAX_FAMILY_SIZE allows -- the
        // minimal discriminating fixture that must trigger truncation.
        const IMPLEMENTOR_COUNT: usize = MAX_FAMILY_SIZE + 5;

        // Every fixture file below declares its OWN package so
        // import-context narrowing collapses to just the interface's own
        // candidate (its package alone matches the caller's) BEFORE family
        // expansion runs -- mirroring the real AC1 scenario family
        // expansion exists for (`resolve.rs`'s own fixtures). Without this,
        // every implementor would already be an un-narrowed candidate and
        // `apply_inheritance_family_expansion`'s symbol-dedup would never
        // freshly add (and thus never mark truncated) any of them.
        // Sentinel local-symbol index for a file's package declaration --
        // mirrors `mod.rs`'s own `PACKAGE_DECL_LOCAL_ID` test constant.
        const PACKAGE_DECL_LOCAL_ID: u32 = 999;
        fn package_decl(file_id: u32, name: &str) -> Declaration {
            Declaration {
                kind: DeclarationKind::Package,
                name: name.to_string(),
                line: 1,
                symbol: make_symbol_id(file_id, PACKAGE_DECL_LOCAL_ID),
                param_count: None,
                param_types: Vec::new(),
                is_varargs: false,
                vararg_index: None,
            }
        }

        let mut interface_file = LocalIndex::new();
        interface_file
            .declarations
            .push(package_decl(INTERFACE_FILE_ID, "pkg.a"));
        interface_file
            .declarations
            .push(method_decl("save", INTERFACE_FILE_ID, 1, Some(0)));
        interface_file.interface_names.push("Repo".to_string());
        interface_file.method_owners.push(MethodOwnerRecord {
            method_symbol: make_symbol_id(INTERFACE_FILE_ID, 1),
            enclosing_type: "Repo".to_string(),
        });

        let mut caller = LocalIndex::new();
        caller
            .declarations
            .push(package_decl(CALLER_FILE_ID, "pkg.a"));
        caller.invocations.push(invocation("save", Some(0)));

        let mut files = vec![
            file(INTERFACE_FILE_ID, "java", interface_file),
            file(CALLER_FILE_ID, "java", caller),
        ];
        for i in 0..IMPLEMENTOR_COUNT {
            let file_id = IMPL_FILE_ID_BASE + i as u32;
            let impl_type_name = format!("Impl{i}");
            let mut impl_file = LocalIndex::new();
            impl_file
                .declarations
                .push(package_decl(file_id, &format!("pkg.impl{i}")));
            impl_file
                .declarations
                .push(method_decl("save", file_id, 1, Some(0)));
            impl_file.method_owners.push(MethodOwnerRecord {
                method_symbol: make_symbol_id(file_id, 1),
                enclosing_type: impl_type_name.clone(),
            });
            impl_file.inheritance.push(InheritanceRecord {
                kind: InheritanceKind::Implements,
                subtype_name: impl_type_name,
                supertype_name: "Repo".to_string(),
                line: 1,
            });
            files.push(file(file_id, "java", impl_file));
        }
        files
    }

    /// Memory-safety amendment: `apply_inheritance_family_expansion`'s
    /// `MAX_FAMILY_SIZE` cap truncating a family anywhere in the bind must
    /// surface at the WHOLE-GRAPH level as `AnalysisCompleteness::
    /// ResolutionAmbiguous` -- even under an `unlimited()` `IndexBudget`
    /// that never exceeds its own (unrelated) raw-candidate ceiling. This
    /// proves the two completeness signals are independent: a family cap
    /// is a resolution-time concern, not a budget-ladder concern, so it
    /// must be reported even when the ladder itself never engages.
    #[test]
    fn family_truncation_reports_resolution_ambiguous_completeness_even_under_an_unlimited_budget()
    {
        let (prepared, _stats) = prepare_bind(family_truncation_fixture(), true);
        let (graph, facts) = finish_bind(prepared, &IndexBudget::unlimited(), &HashMap::new());

        assert_eq!(
            graph.completeness(),
            crate::graph::budget::AnalysisCompleteness::ResolutionAmbiguous,
            "a truncated inheritance family must be visible at the whole-graph completeness level"
        );
        assert!(!facts.index_budget_exceeded, "an unlimited budget must never report exceeded");
        assert!(facts.family_truncated, "BindTimeFacts must independently record the family truncation");
    }

    /// Bug #1897 P1 (dual-review reject), THE central discriminating test
    /// for this specific fix: a SINGLE `finish_bind` call whose two
    /// bind-time facts are BOTH true at once -- `index_budget_exceeded`
    /// (a deliberately zero-ceiling `IndexBudget::new(0, 1)`, which any
    /// non-empty candidate resolution trips) AND `family_truncated` (the
    /// same `family_truncation_fixture` used above, whose truncation is
    /// driven entirely by `families::MAX_FAMILY_SIZE`, independent of any
    /// external `IndexBudget`).
    ///
    /// Before this fix, `finish_bind` returned only a `CodeGraph`, and its
    /// `if exceeded { .. } else if family_truncated { .. }` chain collapsed
    /// both facts into the single `AnalysisCompleteness::IndexBudgetExceeded`
    /// value -- `family_truncated` was computed, then silently discarded,
    /// never reaching any caller. This is `repo_index.rs`'s own equivalent
    /// test, but isolated at the UNIT level: it proves the fact itself
    /// survives `finish_bind`'s own return value, one layer below where
    /// `repo_index::build_repo_graph` reads it.
    #[test]
    fn finish_bind_reports_both_bind_time_facts_when_budget_and_family_truncation_both_trip_at_once(
    ) {
        let (prepared, _stats) = prepare_bind(family_truncation_fixture(), true);
        let (graph, facts) = finish_bind(prepared, &IndexBudget::new(0, 1), &HashMap::new());

        assert!(
            facts.index_budget_exceeded,
            "fixture sanity: any non-empty candidate resolution must trip a zero-ceiling budget"
        );
        assert!(
            facts.family_truncated,
            "the family truncation must be reported EVEN THOUGH the budget ALSO tripped in the \
             SAME bind -- this is exactly the fact #1897 was filed because it got silently \
             dropped when both conditions held at once"
        );
        // `CodeGraph`'s own single-slot completeness is UNCHANGED by this
        // fix -- it still collapses to whichever branch the `if/else if`
        // chain hits first (budget, checked first). That collapse is
        // correct for `is_definitely_dead_code`'s own suppression, which
        // only needs "not Complete"; `BindTimeFacts` above is the lossless
        // channel a caller that needs BOTH reasons must use instead.
        assert_eq!(
            graph.completeness(),
            crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded
        );
    }
}
