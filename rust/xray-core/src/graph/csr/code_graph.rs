//! `CodeGraph` -- the immutable, query-only CSR graph produced by
//! `CodeGraphBuilder::build` (Story #1787, S2, AC5).
//!
//! Every query here is O(edges)-path-safe: `references()` and
//! `candidates_for()` return borrowed slices, `resolve_symbol()` returns a
//! `SymbolId` (a plain `u64`, Copy) by value, and `resolve_string()` returns
//! `&str` borrowed from the shared string table. None of these allocate.

use super::candidate::Candidate;
use super::reference::Reference;
use super::symbol_table::SymbolTable;
use crate::graph::bind::depth::BinderDepth;
use crate::graph::budget::{AnalysisCompleteness, ReferencedBits};
use crate::graph::identity::SymbolId;
use crate::graph::string_table::StringTable;
use std::collections::HashMap;

/// Immutable, query-only whole-repository code graph. The only way to
/// build one is `CodeGraphBuilder::build` -- there is no public
/// constructor here that could assemble an internally-inconsistent graph
/// (e.g. a `Reference`'s `cand_start`/`cand_len` window pointing outside
/// `candidates`).
pub struct CodeGraph {
    references: Vec<Reference>,
    candidates: Vec<Candidate>,
    strings: StringTable,
    symbols: SymbolTable,
    binder_depths: Vec<BinderDepth>,
    /// AC6: whole-build completeness state (see `crate::graph::budget`).
    completeness: AnalysisCompleteness,
    /// AC6 step 3: the decoupled per-symbol referenced-bit.
    referenced: ReferencedBits,
    /// AC6 step 1: per-symbol cached signature lines, dropped entirely on
    /// a budget-exceeded build.
    signatures: HashMap<u32, String>,
    /// Dual-review defect M2 fix: CSR forward adjacency (callees), built
    /// ONCE here rather than re-scanned per query -- see `super::adjacency`
    /// module docs for why `callees_of`/`strongly_connected_components`
    /// were O(V*E) without this.
    forward_index: super::adjacency::AdjacencyIndex,
    /// M2 fix: CSR reverse adjacency (callers), same rationale as
    /// `forward_index`.
    reverse_index: super::adjacency::AdjacencyIndex,
}

impl CodeGraph {
    /// Crate-internal: called only by `CodeGraphBuilder::build`, which is
    /// the sole place that produces these eight parts together and keeps
    /// them consistent. Builds the M2 forward/reverse adjacency indices
    /// HERE, once, from the same `references`/`candidates`/`symbols` this
    /// constructor already receives -- no change to this function's
    /// public parameter list.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn from_parts(
        references: Vec<Reference>,
        candidates: Vec<Candidate>,
        strings: StringTable,
        symbols: SymbolTable,
        binder_depths: Vec<BinderDepth>,
        completeness: AnalysisCompleteness,
        referenced: ReferencedBits,
        signatures: HashMap<u32, String>,
    ) -> Self {
        let forward_index = super::adjacency::AdjacencyIndex::build_forward(symbols.len(), &references, &candidates);
        let reverse_index = super::adjacency::AdjacencyIndex::build_reverse(symbols.len(), &references, &candidates);
        CodeGraph {
            references,
            candidates,
            strings,
            symbols,
            binder_depths,
            completeness,
            referenced,
            signatures,
            forward_index,
            reverse_index,
        }
    }

    /// AC6: this build's whole-graph completeness state.
    pub fn completeness(&self) -> AnalysisCompleteness {
        self.completeness
    }

    /// AC6 step 3: true once ANY raw candidate (regardless of whether it
    /// survived AC6 step-2 capping into the CSR arena) named this dense
    /// symbol id as its target.
    pub fn is_symbol_referenced(&self, dense_symbol_id: u32) -> bool {
        self.referenced.is_referenced(dense_symbol_id)
    }

    /// AC6 step 1: this symbol's cached AC2 signature line, or `None` if
    /// either the symbol never had one or snippets were dropped under
    /// budget pressure.
    pub fn signature_for(&self, dense_symbol_id: u32) -> Option<&str> {
        self.signatures.get(&dense_symbol_id).map(|s| s.as_str())
    }

    /// AC4: "`BinderDepth` exposed per language on the graph". One entry
    /// per distinct language the binder saw when this graph was built.
    pub fn binder_depths(&self) -> &[BinderDepth] {
        &self.binder_depths
    }

    /// All references in the repository, in build order. Borrowed slice --
    /// safe on an O(edges) query path.
    pub fn references(&self) -> &[Reference] {
        &self.references
    }

    /// The candidate set for one reference, sliced from the SHARED arena
    /// via its `cand_start`/`cand_len` window. Borrowed slice -- safe on an
    /// O(edges) query path.
    ///
    /// Fails loud (Rule 13/15) with a clear message on an out-of-bounds
    /// window rather than a bare slice-index panic, since `Reference` has
    /// public fields and could in principle be constructed by a caller
    /// with values that never came from this graph.
    pub fn candidates_for(&self, reference: &Reference) -> &[Candidate] {
        let start = reference.cand_start as usize;
        let len = reference.cand_len as usize;
        let end = start.checked_add(len).unwrap_or_else(|| {
            panic!("Reference candidate window overflows usize: start={start}, len={len}")
        });
        self.candidates.get(start..end).unwrap_or_else(|| {
            panic!(
                "Reference candidate window [{start}..{end}) is out of bounds for this \
                 CodeGraph's candidate arena (len={}) -- the Reference must come from this \
                 same CodeGraph's own builder",
                self.candidates.len()
            )
        })
    }

    /// M2 fix: every callee (dense symbol id) of `dense_symbol_id`, via
    /// the precomputed CSR forward adjacency index built ONCE in
    /// `from_parts` -- O(out-degree), never a re-scan of `references()`.
    /// Borrowed slice, safe on an O(edges) query path.
    pub fn callees_index(&self, dense_symbol_id: u32) -> &[u32] {
        self.forward_index.edges_of(dense_symbol_id)
    }

    /// M2 fix: every caller (dense symbol id) of `dense_symbol_id`, via
    /// the precomputed CSR reverse adjacency index -- O(in-degree), never
    /// a re-scan of `references()`/`candidates()`.
    pub fn callers_index(&self, dense_symbol_id: u32) -> &[u32] {
        self.reverse_index.edges_of(dense_symbol_id)
    }

    /// Resolves a `Candidate`'s dense symbol id back to the real 64-bit
    /// `SymbolId`. Returned BY VALUE: `SymbolId` is a plain `u64` (Copy),
    /// so this allocates nothing even on an O(edges) query path.
    pub fn resolve_symbol(&self, dense_id: u32) -> SymbolId {
        self.symbols.resolve(dense_id)
    }

    /// Resolves an interned string id back to its text, borrowed from the
    /// shared string table -- never an owned `String`.
    pub fn resolve_string(&self, string_id: u32) -> &str {
        self.strings.resolve(string_id)
    }

    /// Checked counterpart to `resolve_symbol` (ADR-002 Defect 2 fix):
    /// `None` instead of a panic on an out-of-range `dense_id`. Used by the
    /// `GraphHandle` FFI accessor thunk, which must never let a panic cross
    /// the dylib boundary on caller-supplied input.
    pub fn try_resolve_symbol(&self, dense_id: u32) -> Option<SymbolId> {
        self.symbols.try_resolve(dense_id)
    }

    /// Checked counterpart to `resolve_string` (ADR-002 Defect 2 fix):
    /// `None` instead of a panic on an out-of-range `string_id`. Used by
    /// the `GraphHandle` FFI accessor thunk, which must never let a panic
    /// cross the dylib boundary on caller-supplied input.
    pub fn try_resolve_string(&self, string_id: u32) -> Option<&str> {
        self.strings.try_resolve(string_id)
    }

    /// AC7: total number of DISTINCT symbols interned in this graph --
    /// dense ids are contiguous `0..symbol_count()`, so this is what lets
    /// `super::ops::strongly_connected_components` enumerate every node
    /// without this module exposing the `SymbolTable` type itself.
    pub fn symbol_count(&self) -> usize {
        self.symbols.len()
    }

    /// AC7: total number of DISTINCT strings interned in this graph --
    /// mirrors `symbol_count` above for the string table.
    pub fn string_count(&self) -> usize {
        self.strings.len()
    }

    /// Reverse lookup: the dense id `symbol` was interned under in THIS
    /// graph, if any. Lets a caller holding a real 64-bit `SymbolId` (e.g.
    /// from `FileForBind::index`, outside this crate's CSR internals)
    /// query `is_symbol_referenced`/`is_definitely_dead_code`.
    pub fn dense_id_for(&self, symbol: SymbolId) -> Option<u32> {
        self.symbols.dense_id_of(symbol)
    }

    /// Bug #1833 fix: this graph carries NO visibility or entry-point
    /// evidence anywhere in the CSR arena (no modifier bit on `SymbolId`,
    /// `Candidate`, `Reference`, or `Declaration` -- confirmed by
    /// inspection, not assumed). `AnalysisCompleteness::Complete` means
    /// "every file in this repo parsed without degradation"; it does NOT
    /// mean "no caller exists anywhere" -- a library's entire public API
    /// has zero IN-REPO callers by construction, since real callers are
    /// downstream projects outside this repo. The pre-fix code treated
    /// `Complete` + unreferenced as proof of death, which on a real
    /// library (jsoup-global, Bug #1833) flagged 1296/3147 symbols
    /// "definitely dead" -- including documented public API -- purely
    /// because nothing else in the SAME repo happened to call them.
    ///
    /// `Some(false)` ("referenced, not dead") stays unconditional: a real
    /// inbound edge is positive evidence, never invalidated by budget
    /// pressure or an indexing gap. But there is currently no in-repo
    /// signal that can turn an ABSENCE of a reference into a proof of
    /// unreachability, so `Some(true)` is presently unreachable -- an
    /// unreferenced symbol always reports `None` ("cannot determine"),
    /// regardless of `completeness`. This is Epic #1786's mandated
    /// under-report-never-over-report direction, made structural rather
    /// than incidental. A future change MAY reintroduce a genuine
    /// `Some(true)` tier once real evidence (e.g. restricted visibility,
    /// no entry-point shape) is plumbed end-to-end from the extractors --
    /// see Bug #1833's discussion for why that plumbing was deferred
    /// rather than done here, and why inventing a partial signal from
    /// existing data (e.g. text-sniffing the cached AC2 signature line)
    /// was rejected as fabricated certainty.
    pub fn is_definitely_dead_code(&self, dense_symbol_id: u32) -> Option<bool> {
        if self.is_symbol_referenced(dense_symbol_id) {
            return Some(false);
        }
        None
    }

    /// Dual-review defect D1 fix: records that `repo_index::build_repo_graph`
    /// (or any other caller ABOVE the binder that knows about a gap the
    /// binder itself cannot see -- `max_files` truncation, a parse error,
    /// an extractor panic, an unreadable source file) dropped part of the
    /// repository from this build. Only takes effect while `completeness`
    /// is still `Complete`: this never "upgrades" a degraded graph back to
    /// a healthier-looking state, and never clobbers a MORE specific
    /// reason (e.g. the binder ladder's own `IndexBudgetExceeded`) with a
    /// less specific one -- the first-recorded degradation reason wins.
    ///
    /// Bug #1833: `completeness` no longer gates the strongest dead-code
    /// tier. `is_definitely_dead_code` suppresses `Some(true)`
    /// unconditionally now (see its doc comment above) because the graph
    /// carries no visibility/entry-point evidence to back that verdict
    /// regardless of how complete the indexing pass was. The recorded
    /// reason still matters for `completeness()`'s other consumers (e.g.
    /// `repo_index`'s own `fact_graph_complete`/budget-exceeded reporting).
    pub fn downgrade_completeness(&mut self, reason: AnalysisCompleteness) {
        if self.completeness == AnalysisCompleteness::Complete {
            self.completeness = reason;
        }
    }
}

#[cfg(test)]
mod tests {
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
}
