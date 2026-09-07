//! O(V+E) CSR forward/reverse adjacency index (dual-review defect M2 fix).
//!
//! `CodeGraph::callees_of`/`callers_of` used to scan EVERY `Reference` in
//! the repository per call -- `reachable_from` calls `callees_of` once per
//! visited node (O(V*E)), and `strongly_connected_components` called it
//! once per symbol UNCONDITIONALLY (also O(V*E)). At the amendment's own
//! Elasticsearch-scale figures (~215K declarations, ~834K call sites) one
//! `strongly_connected_components()` call was roughly 1.8e11 reference
//! comparisons -- unusable at the scale this story targets, even though it
//! terminates (Rule 14 is satisfied; the problem is complexity, not
//! boundedness).
//!
//! `AdjacencyIndex` is built ONCE, at graph-construction time
//! (`CodeGraph::from_parts`), as a FLAT CSR layout (`offsets` + `edges`)
//! rather than one `Vec<u32>` per symbol -- the same reasoning that made
//! the AC5 single-allocation candidate arena mandatory: at 215K symbols, a
//! `Vec<Vec<u32>>` adjacency list would trade an O(V*E) TIME problem for an
//! O(V) ALLOCATION-COUNT problem. Construction is a standard two-pass
//! counting-sort: pass 1 counts each node's out-degree (forward) or
//! in-degree (reverse) to compute prefix-sum offsets, pass 2 scatters each
//! edge into its slot using a per-node write cursor. Both passes are O(V+E)
//! and allocate exactly three `Vec<u32>` buffers total (`counts`/`edges`,
//! `offsets` reuses `counts`), never one allocation per symbol.
//!
//! `edges_of` is a plain slice index -- O(1) plus the O(degree) slice
//! length, never an owned `String` and never a re-scan of the whole
//! reference table (AC5's "no O(edges)-path query returning an owned
//! `String`" constraint, extended here to "and no O(edges)-path query
//! re-scanning O(edges) data").

use super::candidate::Candidate;
use super::reference::Reference;

/// A single CSR-encoded adjacency direction (either "callees" or
/// "callers"). `offsets` has `n + 1` entries; node `v`'s edges are
/// `edges[offsets[v]..offsets[v + 1]]`.
pub(super) struct AdjacencyIndex {
    offsets: Vec<u32>,
    edges: Vec<u32>,
}

impl AdjacencyIndex {
    /// Every edge target for `node`, in build order. O(1) plus the
    /// O(degree) slice length -- never re-scans `references`/`candidates`.
    /// A `node` at or beyond `offsets.len() - 1` (out of range for the
    /// graph this index was built for) returns an empty slice rather than
    /// panicking -- mirrors `callees_of`'s own pre-fix behavior of
    /// returning nothing for a node the graph never interned.
    pub(super) fn edges_of(&self, node: u32) -> &[u32] {
        let node = node as usize;
        if node + 1 >= self.offsets.len() {
            return &[];
        }
        let start = self.offsets[node] as usize;
        let end = self.offsets[node + 1] as usize;
        &self.edges[start..end]
    }

    /// Builds the FORWARD index (callees): keyed by each `Reference.from`,
    /// targeting every one of that reference's candidates' `symbol()`.
    pub(super) fn build_forward(symbol_count: usize, references: &[Reference], candidates: &[Candidate]) -> Self {
        let pairs = edge_pairs(references, candidates);
        build_from_edges(symbol_count, &pairs)
    }

    /// Builds the REVERSE index (callers): keyed by each candidate's
    /// `symbol()`, targeting the owning reference's `from`. Reuses the
    /// SAME `edge_pairs` extraction as `build_forward` -- just swaps
    /// (key, value) into (value, key) before the shared CSR construction
    /// pass, rather than duplicating the reference/candidate traversal.
    pub(super) fn build_reverse(symbol_count: usize, references: &[Reference], candidates: &[Candidate]) -> Self {
        let pairs: Vec<(u32, u32)> =
            edge_pairs(references, candidates).into_iter().map(|(from, to)| (to, from)).collect();
        build_from_edges(symbol_count, &pairs)
    }
}

/// Extracts every `(reference.from, candidate.symbol())` edge pair in
/// build order -- ONE O(E) pass over `references`/`candidates`, shared by
/// both `build_forward` and `build_reverse` (which only differ in which
/// half of each pair becomes the CSR key).
fn edge_pairs(references: &[Reference], candidates: &[Candidate]) -> Vec<(u32, u32)> {
    let mut pairs = Vec::with_capacity(candidates.len());
    for reference in references {
        let start = reference.cand_start as usize;
        let end = start + reference.cand_len as usize;
        for candidate in &candidates[start..end] {
            pairs.push((reference.from, candidate.symbol()));
        }
    }
    pairs
}

/// Standard two-pass counting-sort CSR construction from a flat list of
/// `(key, value)` edges: pass 1 counts each key's out-degree to compute
/// prefix-sum offsets, pass 2 scatters each edge into its slot via a
/// per-key write cursor (a copy of `offsets`, incremented as each edge is
/// placed). O(V+E) time; allocates exactly three `Vec<u32>` buffers
/// (`offsets`, its `cursor` copy, and `edges`) regardless of graph size --
/// never one allocation per symbol.
fn build_from_edges(symbol_count: usize, pairs: &[(u32, u32)]) -> AdjacencyIndex {
    // Defensive: real binder-produced graphs always keep every
    // Reference.from/Candidate.symbol() strictly below symbol_count (both
    // are interned dense ids from the SAME SymbolTable that produced
    // symbol_count). Some hand-built test graphs use out-of-range
    // sentinel ids that were never meant to be queried via
    // callees_of/callers_of -- sizing off the ACTUAL max key seen (never
    // shrinking below symbol_count) means such data is stored safely
    // instead of panicking, with zero behavior change for any real graph.
    let max_key = pairs.iter().map(|&(key, _value)| key as usize).max();
    let n = match max_key {
        Some(max_key) => symbol_count.max(max_key + 1),
        None => symbol_count,
    };
    let mut offsets = vec![0u32; n + 1];
    for &(key, _value) in pairs {
        offsets[key as usize + 1] += 1;
    }
    for i in 0..n {
        offsets[i + 1] += offsets[i];
    }
    let mut cursor = offsets.clone();
    let mut edges = vec![0u32; pairs.len()];
    for &(key, value) in pairs {
        let slot = cursor[key as usize] as usize;
        edges[slot] = value;
        cursor[key as usize] += 1;
    }
    AdjacencyIndex { offsets, edges }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Builds a tiny reference/candidate set mirroring
    /// `csr::ops::tests::callees_of_returns_targets_of_every_reference_whose_from_matches`:
    /// A -> {B, C, D} (two references), B -> {D} (one reference), C and D
    /// have no outgoing references of their own. Dense ids: A=0, B=1, C=2,
    /// D=3.
    fn sample_graph() -> (Vec<Reference>, Vec<Candidate>) {
        let candidates = vec![
            Candidate::new(1, 0), // ref0 -> B
            Candidate::new(2, 0), // ref1 -> C
            Candidate::new(3, 0), // ref1 -> D (ambiguous with C)
            Candidate::new(3, 0), // ref2 -> D
        ];
        let references = vec![
            Reference { from: 0, file: 1, line: 10, kind: 0, cand_start: 0, cand_len: 1 }, // A -> B
            Reference { from: 0, file: 1, line: 11, kind: 0, cand_start: 1, cand_len: 2 }, // A -> {C, D}
            Reference { from: 1, file: 1, line: 12, kind: 0, cand_start: 3, cand_len: 1 }, // B -> D
        ];
        (references, candidates)
    }

    #[test]
    fn build_forward_groups_edges_by_reference_from() {
        let (references, candidates) = sample_graph();
        let index = AdjacencyIndex::build_forward(4, &references, &candidates);

        let mut a_edges = index.edges_of(0).to_vec();
        a_edges.sort_unstable();
        assert_eq!(a_edges, vec![1, 2, 3], "A's callees must be exactly B, C, D");

        assert_eq!(index.edges_of(1), &[3], "B's only callee is D");
        assert!(index.edges_of(2).is_empty(), "C has no outgoing references");
        assert!(index.edges_of(3).is_empty(), "D has no outgoing references");
    }

    #[test]
    fn build_reverse_groups_edges_by_candidate_symbol() {
        let (references, candidates) = sample_graph();
        let index = AdjacencyIndex::build_reverse(4, &references, &candidates);

        assert!(index.edges_of(0).is_empty(), "nothing calls A");
        assert_eq!(index.edges_of(1), &[0], "only A calls B");
        assert_eq!(index.edges_of(2), &[0], "only A calls C");

        let mut d_callers = index.edges_of(3).to_vec();
        d_callers.sort_unstable();
        assert_eq!(d_callers, vec![0, 1], "both A and B call D");
    }

    #[test]
    fn edges_of_out_of_range_node_is_empty_not_a_panic() {
        let (references, candidates) = sample_graph();
        let index = AdjacencyIndex::build_forward(4, &references, &candidates);

        assert!(index.edges_of(999).is_empty());
    }

    /// Regression guard: `code_graph.rs`'s own
    /// `graph_round_trips_references_candidates_symbols_and_strings` test
    /// uses synthetic `Reference.from` values (10, 11, 12) that exceed its
    /// actual interned-symbol count (2) -- a pre-existing, legitimate test
    /// shortcut for a test that never queries `callees_of`/`callers_of`.
    /// Building the CSR index must NOT assume every key/value is bounded
    /// by the declared `symbol_count`; it must size itself to cover
    /// whatever the real data actually contains instead of panicking.
    #[test]
    fn build_forward_does_not_panic_when_a_key_exceeds_declared_symbol_count() {
        let candidates = vec![Candidate::new(0, 0)];
        let references = vec![Reference { from: 10, file: 1, line: 5, kind: 0, cand_start: 0, cand_len: 1 }];

        let index = AdjacencyIndex::build_forward(2, &references, &candidates);

        assert_eq!(index.edges_of(10), &[0]);
    }

    #[test]
    fn empty_graph_produces_empty_edges_for_every_node() {
        let index = AdjacencyIndex::build_forward(3, &[], &[]);

        assert!(index.edges_of(0).is_empty());
        assert!(index.edges_of(1).is_empty());
        assert!(index.edges_of(2).is_empty());
    }
}
