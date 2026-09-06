//! Shared string table for interned symbol/name strings (Story #1787, S2,
//! AC5): "Interned `u32` ids over ONE shared string table. Queries return
//! `SymbolId` or `&str` borrowed from that table -- no query on an
//! O(edges) path may return an owned `String`."
//!
//! Backing storage for the READ path is ONE contiguous `String` buffer
//! (`buffer`); each interned string is recorded as a `(start, len)` byte
//! range into it. `resolve` slices that buffer and returns `&str` tied to
//! `&self` -- by construction it cannot allocate, since a slice of an
//! existing buffer requires no new allocation. This is what matters for
//! AC5: `resolve` sits on the O(edges) query path (called once per
//! candidate/reference), so it must never allocate.
//!
//! `intern`'s dedup index (`lookup: HashMap<String, u32>`) does hold one
//! owned copy of each DISTINCT interned name -- but that cost is paid once
//! per unique symbol name (bounded by the repo's name-table size, typically
//! thousands), never once per edge (millions). It is not on the read path
//! this AC is protecting.

use std::collections::HashMap;

/// One shared, append-only string table for a whole repository's worth of
/// interned symbol names.
pub struct StringTable {
    /// ONE contiguous buffer holding the bytes of every interned string,
    /// back to back. `resolve` slices directly into this.
    buffer: String,
    /// `(start_byte, len_bytes)` into `buffer`, indexed by interned id.
    spans: Vec<(u32, u32)>,
    /// Dedup index: an already-interned string maps to its existing id
    /// instead of being appended again.
    lookup: HashMap<String, u32>,
}

impl StringTable {
    pub fn new() -> Self {
        StringTable { buffer: String::new(), spans: Vec::new(), lookup: HashMap::new() }
    }

    /// Interns `s`, returning its id. Returns the EXISTING id if `s` was
    /// already interned, never appending a duplicate copy.
    pub fn intern(&mut self, s: &str) -> u32 {
        if let Some(&existing_id) = self.lookup.get(s) {
            return existing_id;
        }
        let start = self.buffer.len() as u32;
        self.buffer.push_str(s);
        let len = s.len() as u32;
        let id = self.spans.len() as u32;
        self.spans.push((start, len));
        self.lookup.insert(s.to_string(), id);
        id
    }

    /// Resolves `id` to the exact string that was interned, borrowed
    /// straight from the shared buffer -- never an owned `String`. Safe for
    /// an O(edges) query path: this call allocates nothing.
    ///
    /// Panics loudly (Rule 13, anti-silent-failure) if `id` was never
    /// produced by `intern` on THIS table -- a foreign or corrupt id is a
    /// caller bug, not a condition to silently paper over.
    pub fn resolve(&self, id: u32) -> &str {
        let (start, len) = *self
            .spans
            .get(id as usize)
            .unwrap_or_else(|| panic!("StringTable::resolve: id {id} was not interned in this table"));
        &self.buffer[start as usize..(start + len) as usize]
    }

    /// Number of DISTINCT strings interned so far.
    pub fn len(&self) -> usize {
        self.spans.len()
    }

    pub fn is_empty(&self) -> bool {
        self.spans.is_empty()
    }
}

impl Default for StringTable {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interning_the_same_string_twice_returns_the_same_id() {
        let mut table = StringTable::new();
        let id_a = table.intern("com.example.Foo");
        let id_b = table.intern("com.example.Foo");
        assert_eq!(id_a, id_b);
    }

    #[test]
    fn interning_different_strings_returns_different_ids() {
        let mut table = StringTable::new();
        let id_a = table.intern("Foo");
        let id_b = table.intern("Bar");
        assert_ne!(id_a, id_b);
    }

    #[test]
    fn resolve_returns_the_exact_interned_text() {
        let mut table = StringTable::new();
        let id = table.intern("com.example.Foo");
        assert_eq!(table.resolve(id), "com.example.Foo");
    }

    /// AC5's discriminating requirement: "no query on an O(edges) path may
    /// return an owned `String`". A wrong implementation shaped like
    /// `fn resolve(&self, id: u32) -> String` (cloning out of a
    /// `Vec<String>`) would allocate on every single call -- across ~2M
    /// candidate edges that is exactly the allocator pressure AC5 exists to
    /// eliminate. Because the table is backed by ONE contiguous buffer, a
    /// borrowed slice's pointer must fall INSIDE that buffer's own storage;
    /// an owned copy would live at an unrelated heap address the caller
    /// allocated just now, never inside the table.
    #[test]
    fn resolve_borrows_from_the_shared_buffer_without_allocating() {
        let mut table = StringTable::new();
        let id = table.intern("com.example.Foo");
        let resolved: &str = table.resolve(id);

        let buffer_start = table.buffer.as_ptr();
        let buffer_end = unsafe { buffer_start.add(table.buffer.len()) };
        let resolved_ptr = resolved.as_ptr();

        assert!(
            resolved_ptr >= buffer_start && resolved_ptr < buffer_end,
            "resolve() must return a slice borrowed from the table's own buffer, \
             not a freshly allocated String"
        );
    }

    #[test]
    fn empty_table_has_length_zero() {
        assert_eq!(StringTable::new().len(), 0);
    }

    #[test]
    fn interning_a_string_twice_does_not_grow_the_table() {
        let mut table = StringTable::new();
        table.intern("Foo");
        table.intern("Foo");
        assert_eq!(table.len(), 1);
    }
}
