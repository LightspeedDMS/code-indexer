//! `ReferencedBits` -- the AC6 load-bearing "decoupled per-symbol
//! referenced-bit that survives edge-list truncation" (Story #1787, S2).
//!
//! `mark` is called for EVERY candidate a binder ever proposes, BEFORE any
//! AC6 ladder capping decides which of those candidates actually survive
//! into the CSR `candidates` arena (see `super::super::bind::bind_with_
//! budget`). That ordering is what makes the guarantee true: a symbol
//! whose only proposed edge gets capped away for memory reasons is STILL
//! `is_referenced() == true` here, because this bit was set from the RAW
//! candidate list, never from the (possibly truncated) arena.

/// A grow-on-write bitset over dense per-graph symbol ids. `mark` grows the
/// backing `Vec` on demand rather than requiring a pre-known symbol count
/// up front, since candidates (and therefore symbol interning) are still
/// being discovered while this is populated.
#[derive(Debug, Clone, Default)]
pub struct ReferencedBits {
    bits: Vec<bool>,
}

impl ReferencedBits {
    pub fn new() -> Self {
        ReferencedBits { bits: Vec::new() }
    }

    /// Records that `dense_symbol_id` has at least one inbound edge.
    /// Idempotent: marking the same id twice leaves it `true`.
    pub fn mark(&mut self, dense_symbol_id: u32) {
        let index = dense_symbol_id as usize;
        if index >= self.bits.len() {
            self.bits.resize(index + 1, false);
        }
        self.bits[index] = true;
    }

    /// True once `mark(dense_symbol_id)` has been called at least once.
    /// An id never marked (including one past the end of the backing
    /// `Vec`) is `false` -- never a panic, since a caller may legitimately
    /// query a dense id that this graph's binder never happened to mark.
    pub fn is_referenced(&self, dense_symbol_id: u32) -> bool {
        self.bits.get(dense_symbol_id as usize).copied().unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::super::ReferencedBits;

    #[test]
    fn unmarked_id_is_not_referenced() {
        let bits = ReferencedBits::new();
        assert!(!bits.is_referenced(0));
        assert!(!bits.is_referenced(41));
    }

    #[test]
    fn marking_an_id_makes_it_referenced_without_affecting_others() {
        let mut bits = ReferencedBits::new();
        bits.mark(3);
        assert!(bits.is_referenced(3));
        assert!(!bits.is_referenced(0));
        assert!(!bits.is_referenced(4));
    }

    #[test]
    fn marking_the_same_id_twice_is_idempotent() {
        let mut bits = ReferencedBits::new();
        bits.mark(2);
        bits.mark(2);
        assert!(bits.is_referenced(2));
    }
}
