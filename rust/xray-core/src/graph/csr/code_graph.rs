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
use crate::graph::identity::SymbolId;
use crate::graph::string_table::StringTable;

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
}

impl CodeGraph {
    /// Crate-internal: called only by `CodeGraphBuilder::build`, which is
    /// the sole place that produces these five parts together and keeps
    /// them consistent.
    pub(super) fn from_parts(
        references: Vec<Reference>,
        candidates: Vec<Candidate>,
        strings: StringTable,
        symbols: SymbolTable,
        binder_depths: Vec<BinderDepth>,
    ) -> Self {
        CodeGraph { references, candidates, strings, symbols, binder_depths }
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
}
