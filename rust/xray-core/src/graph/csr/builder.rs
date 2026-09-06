//! `CodeGraphBuilder` -- assembles the CSR arena (Story #1787, S2, AC5).
//!
//! The candidate arena (`candidates: Vec<Candidate>`) is reserved to its
//! FINAL size ONCE, up front, via `with_candidate_capacity`. Every
//! `add_reference` call afterward only appends into that pre-reserved
//! buffer -- it never grows past the reserved capacity, so the whole
//! repository's candidates live in exactly one heap allocation.

use super::candidate::Candidate;
use super::reference::Reference;
use super::symbol_table::SymbolTable;
use crate::graph::string_table::StringTable;

/// Assembles a `CodeGraph`'s CSR arena. See module docs for the
/// single-allocation guarantee this exists to provide.
pub struct CodeGraphBuilder {
    candidates: Vec<Candidate>,
    references: Vec<Reference>,
    strings: StringTable,
    symbols: SymbolTable,
}

impl CodeGraphBuilder {
    /// `total_candidates` MUST be the exact final candidate count for the
    /// whole repository -- reserved once, here, so no later
    /// `add_reference` call ever triggers a reallocation.
    pub fn with_candidate_capacity(total_candidates: usize) -> Self {
        CodeGraphBuilder {
            candidates: Vec::with_capacity(total_candidates),
            references: Vec::new(),
            strings: StringTable::new(),
            symbols: SymbolTable::new(),
        }
    }

    /// Interns a symbol NAME string, returning its dense id in the shared
    /// string table (see `super::super::string_table::StringTable`).
    pub fn intern_string(&mut self, s: &str) -> u32 {
        self.strings.intern(s)
    }

    /// Interns a real 64-bit `SymbolId`, returning the dense id
    /// `Candidate::new`'s `symbol` parameter expects.
    pub fn intern_symbol(&mut self, symbol: crate::graph::identity::SymbolId) -> u32 {
        self.symbols.intern(symbol)
    }

    /// Appends one reference and its candidate set into the shared arena.
    ///
    /// Panics loudly (Rule 13, anti-silent-failure) rather than silently
    /// truncating via `as u16` if `candidates.len()` exceeds `u16::MAX` --
    /// AC6's budget ladder (out of scope here) is what is supposed to cap
    /// candidate-set size before this point is ever reached.
    pub fn add_reference(&mut self, from: u32, file: u32, line: u32, kind: u8, candidates: &[Candidate]) {
        let cand_start = self.candidates.len() as u32;
        let cand_len = u16::try_from(candidates.len()).unwrap_or_else(|_| {
            panic!(
                "reference candidate set has {} entries, exceeding u16::MAX -- \
                 the AC6 budget ladder must cap this before it reaches add_reference",
                candidates.len()
            )
        });
        self.candidates.extend_from_slice(candidates);
        self.references.push(Reference { from, file, line, kind, cand_start, cand_len });
    }

    /// Consumes the builder into an immutable, query-only `CodeGraph`.
    pub fn build(self) -> super::code_graph::CodeGraph {
        super::code_graph::CodeGraph::from_parts(
            self.references,
            self.candidates,
            self.strings,
            self.symbols,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn candidates(n: usize) -> Vec<Candidate> {
        vec![Candidate::new(0, 0); n]
    }

    /// AC5's central discriminating test: "candidates live in ONE flat
    /// allocation, not one Vec per reference". A wrong implementation
    /// shaped like `Vec<Vec<Candidate>>` (one per reference) cannot even
    /// satisfy this API (there is exactly one `candidates` buffer to probe).
    /// A wrong implementation that used an UNRESERVED `Vec::new()` growing
    /// via repeated reallocation would move the buffer at least once as it
    /// grows past its current capacity -- this test builds with the EXACT
    /// final size reserved up front and proves the pointer never moves and
    /// the capacity never exceeds that reservation across many appends.
    #[test]
    fn candidate_arena_is_a_single_allocation_across_many_references() {
        let per_reference_counts = [2usize, 0, 3, 1, 0, 4];
        let total: usize = per_reference_counts.iter().sum();

        let mut builder = CodeGraphBuilder::with_candidate_capacity(total);
        let initial_ptr = builder.candidates.as_ptr();
        let initial_capacity = builder.candidates.capacity();
        assert_eq!(initial_capacity, total);

        for (i, &count) in per_reference_counts.iter().enumerate() {
            builder.add_reference(i as u32, 0, 1, 0, &candidates(count));
            assert_eq!(
                builder.candidates.as_ptr(),
                initial_ptr,
                "candidate arena reallocated (moved) after appending reference {i}"
            );
            assert_eq!(
                builder.candidates.capacity(),
                initial_capacity,
                "candidate arena capacity changed after appending reference {i}"
            );
        }
        assert_eq!(builder.candidates.len(), total);
    }

    #[test]
    fn add_reference_records_the_correct_cand_start_and_cand_len_window() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
        builder.add_reference(10, 0, 1, 0, &candidates(2));
        builder.add_reference(11, 0, 2, 0, &candidates(1));

        assert_eq!(builder.references[0].cand_start, 0);
        assert_eq!(builder.references[0].cand_len, 2);
        assert_eq!(builder.references[1].cand_start, 2);
        assert_eq!(builder.references[1].cand_len, 1);
    }

    /// AC4: "a reference whose definition is outside the repository carries
    /// an EMPTY candidate set (cand_len == 0)".
    #[test]
    fn out_of_repo_reference_records_zero_length_candidate_window() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        builder.add_reference(5, 0, 1, 0, &[]);
        assert_eq!(builder.references[0].cand_len, 0);
        assert!(builder.references[0].is_unresolved());
    }

    /// Guards against silent truncation (Rule 13, anti-silent-failure): if
    /// a caller ever passes more than `u16::MAX` candidates for a single
    /// reference (before AC6's budget ladder exists to cap it), `as u16`
    /// would silently wrap the count instead of failing loud.
    #[test]
    #[should_panic]
    fn add_reference_panics_rather_than_silently_truncating_an_oversized_candidate_set() {
        let too_many = u16::MAX as usize + 1;
        let mut builder = CodeGraphBuilder::with_candidate_capacity(too_many);
        builder.add_reference(0, 0, 1, 0, &candidates(too_many));
    }
}
