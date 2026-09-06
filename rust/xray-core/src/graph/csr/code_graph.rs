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
}

impl CodeGraph {
    /// Crate-internal: called only by `CodeGraphBuilder::build`, which is
    /// the sole place that produces these eight parts together and keeps
    /// them consistent.
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
        CodeGraph { references, candidates, strings, symbols, binder_depths, completeness, referenced, signatures }
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

    /// AC6: "the 'no reference at all' finding tier is SUPPRESSED under
    /// IndexBudgetExceeded". `Some(false)` ("referenced, not dead") is
    /// always safe to report regardless of completeness -- positive
    /// evidence a symbol has an inbound edge is never invalidated by a
    /// later budget squeeze. `Some(true)` ("definitely dead: no reference
    /// anywhere", the strongest dead-code tier) is only ever reported when
    /// this graph is `Complete`; under `IndexBudgetExceeded` the same
    /// absence of evidence reports `None` (suppressed / unknown) instead
    /// of a false-positive dead-code verdict.
    pub fn is_definitely_dead_code(&self, dense_symbol_id: u32) -> Option<bool> {
        if self.is_symbol_referenced(dense_symbol_id) {
            return Some(false);
        }
        if self.completeness == AnalysisCompleteness::IndexBudgetExceeded {
            return None;
        }
        Some(true)
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
}
