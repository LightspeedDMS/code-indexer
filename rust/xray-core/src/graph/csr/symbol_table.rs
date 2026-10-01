//! Dense per-graph symbol interning table (Story #1787, S2, AC5).
//!
//! Mirrors `super::super::string_table::StringTable`'s pattern exactly, but
//! keyed by the 64-bit `identity::SymbolId` instead of `&str`: a
//! `Candidate.symbol` (AC5, `u32`) is a dense index into this table's
//! `entries: Vec<SymbolId>`, so `CodeGraph::resolve_symbol` can hand back
//! the real `SymbolId` for a candidate without storing 8 bytes per
//! candidate in the arena itself.

use crate::graph::identity::SymbolId;
use std::collections::HashMap;

/// Dense per-graph interning table mapping a compact `u32` id to a real
/// 64-bit `SymbolId`.
pub struct SymbolTable {
    /// Dense array: index = interned id, value = the real `SymbolId`.
    entries: Vec<SymbolId>,
    /// Dedup index: an already-interned `SymbolId` maps to its existing
    /// dense id instead of appending a duplicate entry. `SymbolId` is a
    /// plain `u64` (Copy), so this map holds no heap-allocated keys.
    lookup: HashMap<SymbolId, u32>,
}

impl SymbolTable {
    pub fn new() -> Self {
        SymbolTable { entries: Vec::new(), lookup: HashMap::new() }
    }

    /// Interns `symbol`, returning its dense id. Returns the EXISTING dense
    /// id if `symbol` was already interned.
    pub fn intern(&mut self, symbol: SymbolId) -> u32 {
        if let Some(&existing_id) = self.lookup.get(&symbol) {
            return existing_id;
        }
        let id = self.entries.len() as u32;
        self.entries.push(symbol);
        self.lookup.insert(symbol, id);
        id
    }

    /// Resolves a dense id back to the real `SymbolId`. Returns the value
    /// by copy (`SymbolId` is a plain `u64`), so this is safe to call on an
    /// O(edges) query path -- no heap allocation either way.
    ///
    /// Panics loudly (Rule 13, anti-silent-failure) if `id` was never
    /// produced by `intern` on THIS table.
    pub fn resolve(&self, id: u32) -> SymbolId {
        *self
            .entries
            .get(id as usize)
            .unwrap_or_else(|| panic!("SymbolTable::resolve: id {id} was not interned in this table"))
    }

    /// Checked counterpart to `resolve` (ADR-002 Defect 2 fix): returns
    /// `None` instead of panicking when `id` was never interned in this
    /// table. Used by the `GraphHandle` FFI accessor thunks, which must
    /// never let a panic cross the dylib boundary on caller-supplied
    /// (potentially out-of-range) input. `resolve`'s own panic contract is
    /// unchanged and stays in place for the many internal call sites that
    /// pass only internally-known-good ids.
    pub fn try_resolve(&self, id: u32) -> Option<SymbolId> {
        self.entries.get(id as usize).copied()
    }

    /// Reverse lookup: the dense id `symbol` was interned under, if any.
    /// AC6: `CodeGraph::dense_id_for` needs this to let a caller holding a
    /// real 64-bit `SymbolId` (e.g. from `FileForBind`) query
    /// `is_symbol_referenced`/`is_definitely_dead_code`, which are keyed by
    /// dense id like every other CSR query surface.
    pub fn dense_id_of(&self, symbol: SymbolId) -> Option<u32> {
        self.lookup.get(&symbol).copied()
    }

    /// Number of DISTINCT symbols interned so far.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

impl Default for SymbolTable {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interning_the_same_symbol_twice_returns_the_same_id() {
        let mut table = SymbolTable::new();
        let a = table.intern(0xAAAA_BBBB_0000_0001);
        let b = table.intern(0xAAAA_BBBB_0000_0001);
        assert_eq!(a, b);
    }

    #[test]
    fn interning_different_symbols_returns_different_ids() {
        let mut table = SymbolTable::new();
        let a = table.intern(1);
        let b = table.intern(2);
        assert_ne!(a, b);
    }

    #[test]
    fn resolve_returns_the_exact_symbol_id_that_was_interned() {
        let mut table = SymbolTable::new();
        let real_symbol: SymbolId = 0x0000_0007_0000_002A;
        let dense_id = table.intern(real_symbol);
        assert_eq!(table.resolve(dense_id), real_symbol);
    }

    #[test]
    fn interning_a_symbol_twice_does_not_grow_the_table() {
        let mut table = SymbolTable::new();
        table.intern(42);
        table.intern(42);
        assert_eq!(table.len(), 1);
    }

    /// Defect 2 (ADR-002 GraphHandle FFI fix): `try_resolve` is the checked
    /// counterpart to `resolve` -- it must discriminate a genuinely
    /// interned id (`Some`) from an out-of-range one (`None`) without
    /// panicking, so the GraphHandle accessor thunks built on top of it
    /// never let a panic cross the dylib boundary. `resolve`'s own panic
    /// contract is UNCHANGED and still covered by
    /// `resolve_returns_the_exact_symbol_id_that_was_interned` above.
    #[test]
    fn try_resolve_returns_some_for_interned_id_and_none_for_out_of_range_id() {
        let mut table = SymbolTable::new();
        let real_symbol: SymbolId = 0x0000_0007_0000_002A;
        let dense_id = table.intern(real_symbol);

        assert_eq!(table.try_resolve(dense_id), Some(real_symbol));
        assert_eq!(table.try_resolve(u32::MAX), None, "an id never interned in this table must return None, never panic");
    }
}
