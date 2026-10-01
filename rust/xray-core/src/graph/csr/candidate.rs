//! `Candidate` -- one proposed definition for a `Reference` (Story #1787,
//! S2, AC4/AC5). Field types match the CSR layout exactly (`symbol: u32`,
//! `confidence: u8`, `reasons: u16`).
//!
//! `symbol` is a DENSE, per-graph interned id (see
//! `super::symbol_table::SymbolTable`) -- not the full 64-bit
//! `identity::SymbolId` directly. AC5 says "Interned `u32` ids over ONE
//! shared string table. Queries return `SymbolId` ... borrowed from that
//! table" -- the same interning pattern applies to symbols as to strings:
//! storing a 64-bit `SymbolId` per candidate would cost 8 bytes/candidate
//! instead of 4 across millions of candidates, so the arena stores the
//! compact interned id and `CodeGraph::resolve_symbol` looks up the real
//! `SymbolId` on demand.
//!
//! The ONLY constructor is `Candidate::new`, which always computes
//! `confidence` via `Confidence::derive(reasons)` -- there is no way to
//! build a `Candidate` whose `confidence` disagrees with its `reasons`.

use super::super::confidence::Confidence;

/// One proposed definition for a `Reference`, with the evidence
/// (`reasons`) that produced it and the confidence level DERIVED from that
/// evidence. Fields are private: `new` is the only way to build one, so
/// `confidence` can never disagree with `reasons`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Candidate {
    symbol: u32,
    confidence: u8,
    reasons: u16,
}

impl Candidate {
    /// Builds a `Candidate`, computing `confidence` from `reasons` via
    /// `Confidence::derive` -- the only path by which this crate ever
    /// produces a `Candidate`.
    pub fn new(symbol: u32, reasons: u16) -> Candidate {
        Candidate { symbol, confidence: Confidence::derive(reasons) as u8, reasons }
    }

    /// The candidate's dense, per-graph interned symbol id (see module docs
    /// for why this is not the full 64-bit `SymbolId`).
    pub fn symbol(&self) -> u32 {
        self.symbol
    }

    pub fn confidence(&self) -> Confidence {
        Confidence::from_u8(self.confidence)
    }

    pub fn reasons(&self) -> u16 {
        self.reasons
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::reasons;

    /// The invariant AC4 actually requires: for EVERY `Candidate` this
    /// crate can construct, `.confidence()` must equal
    /// `Confidence::derive(.reasons())`. Since `Candidate::new` is the only
    /// constructor and always calls `derive` internally, this is really a
    /// regression guard against someone later adding a second constructor
    /// that accepts `confidence` and `reasons` as independent parameters.
    #[test]
    fn confidence_always_agrees_with_derive_of_reasons() {
        for &reasons_bits in reasons::ALL_FLAGS {
            let candidate = Candidate::new(42, reasons_bits);
            assert_eq!(candidate.confidence(), Confidence::derive(reasons_bits));
        }
    }

    #[test]
    fn accessors_return_the_values_passed_to_new() {
        let candidate = Candidate::new(7, reasons::SAME_FILE);
        assert_eq!(candidate.symbol(), 7);
        assert_eq!(candidate.reasons(), reasons::SAME_FILE);
        assert_eq!(candidate.confidence(), Confidence::SameFile);
    }
}
