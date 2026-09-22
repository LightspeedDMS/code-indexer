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
    /// Bug #1900 (epic #1906 P2): parallel to `edges` -- `ambiguous[i]` is
    /// true when the `Reference` that contributed `edges[i]` had MORE THAN
    /// ONE surviving candidate in its window (`cand_len > 1`, via
    /// `Reference::is_ambiguous`). Populated for both directions (forward
    /// and reverse) by the same `build_from_edges` pass, though only the
    /// FORWARD index is ever queried for it today (`CodeGraph::edge_reason`,
    /// directional by construction). Kept on both to avoid a second,
    /// divergent `AdjacencyIndex` shape for one direction only.
    ///
    /// Bug #1900 review round 2 (BLOCKING P2): this is a COUNT-based
    /// property of the reference's candidate window -- it says nothing
    /// about which EVIDENCE bits (`graph::reasons`) actually backed
    /// `edges[i]`. A reference with exactly one surviving candidate can
    /// still be a fabricated resolution (e.g. a same-named `put(2 params)`
    /// method landed on by arity/package heuristics alone, no
    /// `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO`) -- `ambiguous` alone
    /// cannot distinguish that from a truly unique, provably-correct
    /// resolution. See `reasons` below, which carries the REAL evidence
    /// bits per occurrence for exactly that reason.
    ambiguous: Vec<bool>,
    /// Bug #1900 (epic #1906 P2, review round 2): parallel to `edges` --
    /// `reasons[i]` is the CANDIDATE's own `Candidate::reasons()` bitmask
    /// (`graph::reasons::*`) for the specific candidate that contributed
    /// `edges[i]`. This is the provenance `edge_evidence` needs: `ambiguous`
    /// answers "how many candidates survived at this call site", while
    /// `reasons` answers "what evidence actually backed the surviving
    /// candidate this edge occurrence came from" -- e.g. whether
    /// `RECEIVER_TYPE_MATCH` or `UNIQUE_NAME_IN_REPO` fired, versus only
    /// weaker structural bits like `SAME_PACKAGE`/`ARITY_MATCH`. Populated
    /// for both directions by the same `build_from_edges` pass, mirroring
    /// `ambiguous` exactly.
    ///
    /// Bug #1900 review round 3 (coordinator-authorized): NO LONGER
    /// populated for both directions -- see `has_evidence` below. This doc
    /// comment's "populated for both directions" is now historical; kept
    /// for the rationale trail, not the current behavior.
    reasons: Vec<u16>,
    /// Bug #1900 review round 3 (coordinator-authorized): true when
    /// `ambiguous`/`reasons` were ACTUALLY populated for this index --
    /// `true` for the forward index, `false` for the reverse index.
    /// `edge_reason`/`edge_evidence` are directional by construction and
    /// only ever read `CodeGraph::forward_index`, so populating
    /// `ambiguous`/`reasons` on the reverse index was pure dead weight:
    /// at repo-B's measured 10.2M out-edges, that is 10.2M x (1 byte
    /// `bool` + 2 bytes `u16`) = ~30.6 MB per analyze child that was
    /// allocated, written, and never read. `build_reverse` now leaves
    /// `ambiguous`/`reasons` as empty `Vec`s (`has_evidence: false`);
    /// `edges`/`offsets` (what `callers_of` actually reads) are populated
    /// exactly as before on both directions -- this field affects ONLY the
    /// two evidence arrays, never edge presence/correctness.
    ///
    /// `evidence_for_edge`/`filtered_edges_of` check this FIRST and return
    /// `None`/empty immediately when `false`, before ever indexing into
    /// `reasons` -- chosen over a type-level "impossible to call" split
    /// (e.g. separate `Forward`/`Reverse` marker types) because these
    /// methods are `pub(super)`, reachable only from `code_graph.rs`,
    /// which already calls the unfiltered pair exclusively on
    /// `forward_index`; a fail-safe runtime guard gives the same
    /// panic-proof guarantee (no out-of-bounds read of a
    /// shorter-than-`edges` array -- the same defect class as the
    /// `file_string_id` panic fixed earlier in this epic) without a
    /// larger, unrequested type redesign of a crate-internal module
    /// (Rule 9, anti-divergent-creativity).
    has_evidence: bool,
    /// #1924/#1925: SEPARATE from `has_evidence` -- true only when
    /// `ambiguous` was ALSO populated (the forward index only;
    /// `build_reverse_with_evidence` populates `reasons` for `filtered_
    /// edges_of` but has no consumer for `ambiguous` at all, so it is
    /// left empty). `ambiguous_for_edge` checks THIS flag, never
    /// `has_evidence`, so it can never index into an empty `ambiguous`
    /// array on a reasons-only index.
    has_ambiguous: bool,
    /// #1924/#1925 (P3): test-only work-count instrumentation, incremented
    /// by `end - start` (the number of occurrences examined) on every
    /// `filtered_edges_of` call -- proves the per-query scan is bounded by
    /// THIS node's own CSR range, never by any OTHER node's (e.g. a
    /// caller's own forward out-degree). Absent from a non-test build
    /// entirely (zero size, zero cost).
    #[cfg(test)]
    scan_count: std::sync::atomic::AtomicUsize,
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

    /// #1924/#1925: every edge target for `node` with AT LEAST ONE
    /// contributing OCCURRENCE whose OWN evidence bits
    /// (never merged with any other occurrence of the same target) satisfy
    /// `(bits & required) == required && (bits & forbidden) == 0`.
    ///
    /// **Why per-occurrence, not merged-then-masked (the pre-rework bug)**:
    /// merging every occurrence's bits with OR BEFORE applying the filter
    /// makes a genuinely evidenced edge indistinguishable from a
    /// COINCIDENTALLY co-occurring fabricated one. Reproduced end-to-end: a
    /// caller with a real `helper.equals(x)` call (receiver `Helper`,
    /// tagging `Helper.equals` with `RECEIVER_TYPE_MATCH`) and a SEPARATE,
    /// fabricated `text.equals(y)` call in the SAME method (receiver
    /// `String`, the bare-name/arity fallback ALSO binding to `Helper.
    /// equals`, tagging `RECEIVER_TYPE_MISMATCH`) contribute TWO occurrences
    /// of the identical `(caller -> Helper.equals)` pair. Merging them
    /// (`MATCH | MISMATCH`) and THEN checking `forbidden: MISMATCH` drops
    /// the edge entirely -- even though the genuine occurrence, evaluated
    /// on its own, would have satisfied the filter. Checking each
    /// occurrence's OWN bits independently (this implementation) keeps the
    /// edge: the real `MATCH`-only occurrence alone qualifies.
    ///
    /// A target qualifying via ANY occurrence appears in the result
    /// EXACTLY ONCE, in FIRST-SEEN CSR order (never a `HashMap`'s
    /// unspecified iteration order) -- deliberately NOT identical to
    /// `callees_of`/`callers_of`, which return one entry PER OCCURRENCE
    /// (duplicates included) in raw CSR order; this method always
    /// deduplicates by target. `required: 0, forbidden: 0` therefore
    /// returns the same SET of targets as the unfiltered accessor, just
    /// deduplicated -- never assume identical `Vec` length or order.
    ///
    /// O(out-degree of `node`) -- ONE pass over `node`'s own CSR slice,
    /// never a per-target `evidence_for_edge` call (which would cost
    /// O(out-degree^2): see `edge_evidence`'s own doc comment
    /// (`code_graph.rs`) for the exact pathological shape this avoids, the
    /// same complexity class Bug #1900 M2 already fixed for `callees_of`/
    /// `strongly_connected_components`). Empty (never a panic or a
    /// fabricated edge) when `!has_evidence` or `node` is out of range,
    /// mirroring `edges_of`'s own fail-safe shape.
    /// #1924/#1925 (P3): test-only accessor for `scan_count` -- see that
    /// field's own doc comment.
    #[cfg(test)]
    pub(super) fn scan_count(&self) -> usize {
        self.scan_count.load(std::sync::atomic::Ordering::SeqCst)
    }

    pub(super) fn filtered_edges_of(&self, node: u32, required: u16, forbidden: u16) -> Vec<u32> {
        if !self.has_evidence {
            return Vec::new();
        }
        let node_usize = node as usize;
        if node_usize + 1 >= self.offsets.len() {
            return Vec::new();
        }
        let start = self.offsets[node_usize] as usize;
        let end = self.offsets[node_usize + 1] as usize;
        #[cfg(test)]
        self.scan_count.fetch_add(end - start, std::sync::atomic::Ordering::SeqCst);
        let mut seen: std::collections::HashSet<u32> = std::collections::HashSet::new();
        let mut ordered = Vec::new();
        for i in start..end {
            let bits = self.reasons[i];
            if bits & required != required || bits & forbidden != 0 {
                continue;
            }
            let target = self.edges[i];
            if seen.insert(target) {
                ordered.push(target);
            }
        }
        ordered
    }

    /// Bug #1900: whether the `(node -> target)` edge is backed by AT LEAST
    /// ONE contributing reference whose candidate window held exactly one
    /// surviving candidate (`Some(false)` -- unambiguous, real single-target
    /// evidence) versus every contributing reference offering multiple
    /// candidates (`Some(true)` -- ambiguous only). `None` when `target` is
    /// not among `node`'s edges at all. O(out-degree of `node`) -- scans
    /// only `node`'s own CSR slice, exactly like `edges_of`, never the
    /// whole edge arena.
    pub(super) fn ambiguous_for_edge(&self, node: u32, target: u32) -> Option<bool> {
        if !self.has_ambiguous {
            return None;
        }
        let node_usize = node as usize;
        if node_usize + 1 >= self.offsets.len() {
            return None;
        }
        let start = self.offsets[node_usize] as usize;
        let end = self.offsets[node_usize + 1] as usize;
        let mut found = false;
        for i in start..end {
            if self.edges[i] == target {
                found = true;
                if !self.ambiguous[i] {
                    return Some(false);
                }
            }
        }
        found.then_some(true)
    }

    /// Bug #1900 (epic #1906 P2, review round 2): the REAL evidence half of
    /// this index. Returns the bitwise-OR of `Candidate::reasons()` across
    /// EVERY contributing occurrence of the `(node -> target)` edge --
    /// `None` when `target` is not among `node`'s edges at all, `Some(0)`
    /// when it is but no evidence bit was ever set on any contributing
    /// candidate (a candidate built with an empty reasons mask). Unlike
    /// `ambiguous_for_edge` (which answers "how many candidates survived"),
    /// this answers "what evidence actually backed this edge" -- the
    /// distinction that lets an evaluator require e.g.
    /// `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO` before trusting a hop,
    /// rather than trusting count alone. O(out-degree of `node`) -- scans
    /// only `node`'s own CSR slice, exactly like `edges_of`/
    /// `ambiguous_for_edge`, never the whole edge arena.
    pub(super) fn evidence_for_edge(&self, node: u32, target: u32) -> Option<u16> {
        if !self.has_evidence {
            return None;
        }
        let node_usize = node as usize;
        if node_usize + 1 >= self.offsets.len() {
            return None;
        }
        let start = self.offsets[node_usize] as usize;
        let end = self.offsets[node_usize + 1] as usize;
        let mut found = false;
        let mut evidence = 0u16;
        for i in start..end {
            if self.edges[i] == target {
                found = true;
                evidence |= self.reasons[i];
            }
        }
        found.then_some(evidence)
    }

    /// Builds the FORWARD index (callees): keyed by each `Reference.from`,
    /// targeting every one of that reference's candidates' `symbol()`.
    /// `needs_evidence: true, needs_ambiguous: true` -- this is the ONLY
    /// direction `edge_reason`/`edge_evidence` ever query, so both
    /// `ambiguous`/`reasons` are populated.
    pub(super) fn build_forward(symbol_count: usize, references: &[Reference], candidates: &[Candidate]) -> Self {
        let pairs = edge_pairs(references, candidates);
        build_from_edges(symbol_count, &pairs, true, true)
    }

    /// Builds the REVERSE index (callers): keyed by each candidate's
    /// `symbol()`, targeting the owning reference's `from`. Reuses the
    /// SAME `edge_pairs` extraction as `build_forward` -- just swaps
    /// (key, value) into (value, key) before the shared CSR construction
    /// pass, rather than duplicating the reference/candidate traversal.
    ///
    /// `needs_evidence: false`: nothing ever queries edge tier/evidence on
    /// the plain REVERSE direction, so `ambiguous`/`reasons` are left as
    /// empty `Vec`s -- see `AdjacencyIndex::has_evidence`'s doc comment for
    /// the ~30.6 MB/analyze child this avoids at repo-B's out-edge scale.
    /// `edges`/`offsets` (what `callers_of` reads) are completely
    /// unaffected.
    pub(super) fn build_reverse(symbol_count: usize, references: &[Reference], candidates: &[Candidate]) -> Self {
        Self::build_reverse_impl(symbol_count, references, candidates, false)
    }

    /// #1924/#1925: a SECOND reverse-index constructor that DOES populate
    /// per-occurrence REASONS evidence (never `ambiguous`, which has no
    /// consumer on this direction at all) -- see `CodeGraph::callers_of_
    /// filtered`'s doc comment for why this exists and why it is built
    /// LAZILY (via a `OnceLock`), only for a `CodeGraph` that actually
    /// calls a filtered CALLERS-direction primitive, rather than
    /// unconditionally like `build_reverse`. The ~30.6 MB/analyze-child
    /// cost `has_evidence`'s own doc comment measured for the
    /// unconditional case is real, but this way it is paid ONLY when the
    /// capability is actually used, and only for the ONE array
    /// `filtered_edges_of` reads. This constructor makes `callers_of_
    /// filtered`/`reachable_to_filtered` O(in-degree) per query after a
    /// one-time O(V+E) build, instead of the O(in-degree x avg caller
    /// out-degree) cost of deriving filtered callers by scanning each
    /// caller's own forward slice per reverse occurrence.
    pub(super) fn build_reverse_with_evidence(symbol_count: usize, references: &[Reference], candidates: &[Candidate]) -> Self {
        Self::build_reverse_impl(symbol_count, references, candidates, true)
    }

    /// `needs_evidence` gates ONLY `reasons` (what `filtered_edges_of`/
    /// `evidence_for_edge` read) -- `ambiguous` is always `needs_ambiguous:
    /// false` here, since NEITHER reverse-index constructor has a consumer
    /// for it (see `build_reverse_with_evidence`'s own doc comment).
    fn build_reverse_impl(symbol_count: usize, references: &[Reference], candidates: &[Candidate], needs_evidence: bool) -> Self {
        let pairs: Vec<EdgePair> = edge_pairs(references, candidates)
            .into_iter()
            .map(|(from, to, ambiguous, reasons)| (to, from, ambiguous, reasons))
            .collect();
        build_from_edges(symbol_count, &pairs, needs_evidence, false)
    }
}

/// One extracted `(reference.from, candidate.symbol(), ambiguous, reasons)`
/// edge occurrence. `ambiguous` is `reference.is_ambiguous()` (a property of
/// the REFERENCE's whole candidate window, uniform across every candidate in
/// it); `reasons` is `candidate.reasons()` (a property of the individual
/// CANDIDATE that produced this specific occurrence) -- see
/// `AdjacencyIndex`'s field docs for why these are deliberately different
/// properties, not two views of the same fact.
type EdgePair = (u32, u32, bool, u16);

/// Extracts every `(reference.from, candidate.symbol(), ambiguous, reasons)`
/// edge occurrence in build order -- ONE O(E) pass over
/// `references`/`candidates`, shared by both `build_forward` and
/// `build_reverse` (which only differ in which half of each pair becomes the
/// CSR key). Bug #1900: `ambiguous` is `reference.is_ambiguous()` (Rule 4:
/// reuses the existing predicate rather than re-deriving `cand_len > 1`
/// here) -- true when MORE THAN ONE candidate survived in that reference's
/// own window. `reasons` (review round 2) is the CANDIDATE's own
/// `reasons()` bitmask, the real per-occurrence evidence `edge_evidence`
/// needs and `ambiguous` cannot provide.
fn edge_pairs(references: &[Reference], candidates: &[Candidate]) -> Vec<EdgePair> {
    let mut pairs = Vec::with_capacity(candidates.len());
    for reference in references {
        let start = reference.cand_start as usize;
        let end = start + reference.cand_len as usize;
        let ambiguous = reference.is_ambiguous();
        for candidate in &candidates[start..end] {
            pairs.push((reference.from, candidate.symbol(), ambiguous, candidate.reasons()));
        }
    }
    pairs
}

/// Standard two-pass counting-sort CSR construction from a flat list of
/// `(key, value, ambiguous, reasons)` edges: pass 1 counts each key's
/// out-degree to compute prefix-sum offsets, pass 2 scatters each edge (and,
/// selectively, its `ambiguous` flag and/or `reasons` bitmask, Bug #1900)
/// into its slot via a per-key write cursor (a copy of `offsets`,
/// incremented as each edge is placed). O(V+E) time.
///
/// `needs_reasons` and `needs_ambiguous` are INDEPENDENT: each array is
/// allocated and populated only when its own flag is true, so a caller
/// that needs `reasons` (e.g. `filtered_edges_of`) but never reads
/// `ambiguous` pays for exactly one extra `Vec`, not two. `false, false`
/// (the plain reverse-index caller) allocates exactly three `Vec` buffers
/// (`offsets`, its `cursor` copy, `edges`) instead of five; `true, true`
/// (the forward-index caller) allocates all five, exactly as before.
/// `edges`/`offsets` construction is IDENTICAL regardless of either flag
/// -- neither ever affects edge presence or `callers_of`/`callees_of`
/// correctness, only whether the two evidence arrays exist.
fn build_from_edges(symbol_count: usize, pairs: &[EdgePair], needs_reasons: bool, needs_ambiguous: bool) -> AdjacencyIndex {
    // Defensive: real binder-produced graphs always keep every
    // Reference.from/Candidate.symbol() strictly below symbol_count (both
    // are interned dense ids from the SAME SymbolTable that produced
    // symbol_count). Some hand-built test graphs use out-of-range
    // sentinel ids that were never meant to be queried via
    // callees_of/callers_of -- sizing off the ACTUAL max key seen (never
    // shrinking below symbol_count) means such data is stored safely
    // instead of panicking, with zero behavior change for any real graph.
    let max_key = pairs.iter().map(|&(key, _value, _ambiguous, _reasons)| key as usize).max();
    let n = match max_key {
        Some(max_key) => symbol_count.max(max_key + 1),
        None => symbol_count,
    };
    let mut offsets = vec![0u32; n + 1];
    for &(key, _value, _ambiguous, _reasons) in pairs {
        offsets[key as usize + 1] += 1;
    }
    for i in 0..n {
        offsets[i + 1] += offsets[i];
    }
    let mut cursor = offsets.clone();
    let mut edges = vec![0u32; pairs.len()];
    let mut ambiguous = if needs_ambiguous { vec![false; pairs.len()] } else { Vec::new() };
    let mut reasons = if needs_reasons { vec![0u16; pairs.len()] } else { Vec::new() };
    for &(key, value, edge_ambiguous, edge_reasons) in pairs {
        let slot = cursor[key as usize] as usize;
        edges[slot] = value;
        if needs_ambiguous {
            ambiguous[slot] = edge_ambiguous;
        }
        if needs_reasons {
            reasons[slot] = edge_reasons;
        }
        cursor[key as usize] += 1;
    }
    AdjacencyIndex {
        offsets,
        edges,
        ambiguous,
        reasons,
        has_evidence: needs_reasons,
        has_ambiguous: needs_ambiguous,
        #[cfg(test)]
        scan_count: std::sync::atomic::AtomicUsize::new(0),
    }
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

    /// Bug #1900 (epic #1906 P2, review round 2 -- the CENTRAL discriminating
    /// test for the fabricated-edge fix): `edge_evidence`/`evidence_for_edge`
    /// must OR the REAL `Candidate::reasons()` bits of every contributing
    /// occurrence, never derive anything from `cand_len`. Mirrors the
    /// review's own reproduction shape: a reference resolved to a SINGLE
    /// candidate (so `ambiguous_for_edge` would report `Some(false)`,
    /// "unambiguous") whose reasons carry ONLY weak structural bits
    /// (`SAME_PACKAGE | ARITY_MATCH`, no `RECEIVER_TYPE_MATCH`/
    /// `UNIQUE_NAME_IN_REPO`) -- proving the count-based tier and the
    /// evidence bits are genuinely independent signals.
    #[test]
    fn evidence_for_edge_ors_the_reason_bits_of_every_contributing_occurrence() {
        use crate::graph::reasons;

        let candidates = vec![
            // A -> B: single candidate, weak evidence only (the fabricated-
            // edge shape from the review: no RECEIVER_TYPE_MATCH/
            // UNIQUE_NAME_IN_REPO despite being the sole survivor).
            Candidate::new(1, reasons::SAME_PACKAGE | reasons::ARITY_MATCH),
        ];
        let references = vec![Reference { from: 0, file: 1, line: 10, kind: 0, cand_start: 0, cand_len: 1 }];
        let index = AdjacencyIndex::build_forward(2, &references, &candidates);

        assert_eq!(
            index.evidence_for_edge(0, 1),
            Some(reasons::SAME_PACKAGE | reasons::ARITY_MATCH),
            "evidence must be the candidate's real reasons bitmask, not a count-derived flag"
        );
        assert!(
            index.evidence_for_edge(0, 1).unwrap() & reasons::RECEIVER_TYPE_MATCH == 0,
            "the fabricated-edge fixture carries no RECEIVER_TYPE_MATCH bit -- an evaluator \
             checking for it must be able to tell this hop apart from a truly verified one"
        );
    }

    /// A pair reached by TWO occurrences (two separate references, or two
    /// candidates in one window) must report the OR of BOTH occurrences'
    /// reason bits -- real evidence from either call site counts, mirroring
    /// `edge_reason`'s own "one real call site is enough" mixed-evidence
    /// rule (kept unchanged by this fix).
    #[test]
    fn evidence_for_edge_ors_bits_across_multiple_occurrences_of_the_same_pair() {
        use crate::graph::reasons;

        let candidates = vec![
            Candidate::new(1, reasons::SAME_PACKAGE),
            Candidate::new(1, reasons::UNIQUE_NAME_IN_REPO),
        ];
        let references = vec![
            Reference { from: 0, file: 1, line: 10, kind: 0, cand_start: 0, cand_len: 1 },
            Reference { from: 0, file: 1, line: 11, kind: 0, cand_start: 1, cand_len: 1 },
        ];
        let index = AdjacencyIndex::build_forward(2, &references, &candidates);

        assert_eq!(index.evidence_for_edge(0, 1), Some(reasons::SAME_PACKAGE | reasons::UNIQUE_NAME_IN_REPO));
    }

    #[test]
    fn evidence_for_edge_returns_none_for_a_pair_with_no_edge_at_all() {
        let (references, candidates) = sample_graph();
        let index = AdjacencyIndex::build_forward(4, &references, &candidates);

        assert_eq!(index.evidence_for_edge(0, 999), None, "no edge at all must report None, never a fabricated Some(0)");
    }

    /// Bug #1900 (review round 3, coordinator-authorized): the reverse
    /// index's `ambiguous`/`reasons` arrays are DEAD WEIGHT -- only the
    /// FORWARD index is ever queried for edge tier/evidence
    /// (`CodeGraph::edge_reason`/`edge_evidence`, directional by
    /// construction). This is the CENTRAL discriminating test for skipping
    /// their population on the reverse index:
    ///
    /// 1. `callers_of` (via `edges_of`) must stay byte-for-byte correct on
    ///    a fixture that includes an AMBIGUOUS reference (B has two
    ///    candidates, C and D) -- proving the skip does not silently drop
    ///    or corrupt any edge, only the evidence arrays.
    /// 2. `ambiguous_for_edge`/`evidence_for_edge` called on the reverse
    ///    index must return `None` -- fail-safe, never an out-of-bounds
    ///    read of a shorter-than-`edges` array (the same defect class as
    ///    the `file_string_id` panic fixed earlier in this epic).
    ///
    /// `RED against unmodified code`: today's `build_reverse` DOES
    /// populate `ambiguous`/`reasons`, so `ambiguous_for_edge`/
    /// `evidence_for_edge` on the reverse index currently return
    /// `Some(..)`, not `None` -- this test's `None` assertions fail until
    /// the `needs_evidence` gate is added.
    #[test]
    fn reverse_index_skips_evidence_population_but_still_serves_callers_of_correctly() {
        use crate::graph::reasons;

        // A -> {C, D} ambiguous (two candidates); B -> D sole candidate.
        let candidates = vec![
            Candidate::new(2, reasons::SAME_PACKAGE), // ref0 -> C
            Candidate::new(3, reasons::SAME_PACKAGE), // ref0 -> D (ambiguous with C)
            Candidate::new(3, reasons::UNIQUE_NAME_IN_REPO), // ref1 -> D
        ];
        let references = vec![
            Reference { from: 0, file: 1, line: 10, kind: 0, cand_start: 0, cand_len: 2 }, // A -> {C, D}
            Reference { from: 1, file: 1, line: 11, kind: 0, cand_start: 2, cand_len: 1 }, // B -> D
        ];
        let reverse = AdjacencyIndex::build_reverse(4, &references, &candidates);

        // 1. callers_of must be exactly correct, unaffected by skipping evidence.
        assert!(reverse.edges_of(2).contains(&0), "A must still be recorded as a caller of C");
        let mut d_callers = reverse.edges_of(3).to_vec();
        d_callers.sort_unstable();
        assert_eq!(d_callers, vec![0, 1], "both A and B must still be recorded as callers of D");

        // 2. Evidence accessors on the reverse index must fail safe (None),
        // never read a short/empty vector out of bounds or answer wrong.
        assert_eq!(
            reverse.ambiguous_for_edge(2, 0),
            None,
            "ambiguous_for_edge on the reverse index must return None, never read unpopulated evidence"
        );
        assert_eq!(
            reverse.evidence_for_edge(3, 0),
            None,
            "evidence_for_edge on the reverse index must return None, never read unpopulated evidence"
        );
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

    /// #1924/#1925 rework: `filtered_edges_of` checks EACH occurrence's OWN
    /// evidence bits independently (never merged across occurrences of the
    /// same target) -- this fixture's B is reached by two occurrences
    /// (`SAME_PACKAGE` alone, then `RECEIVER_TYPE_MATCH` alone); the SECOND
    /// occurrence alone already satisfies `required=RECEIVER_TYPE_MATCH,
    /// forbidden=RECEIVER_TYPE_MISMATCH`, so B qualifies. C's single
    /// occurrence carries BOTH bits together and is excluded. Done in ONE
    /// pass over the node's own CSR slice -- see this method's own doc
    /// comment for why calling `evidence_for_edge` once per target here
    /// would reintroduce the exact O(out-degree^2) shape Bug #1900 M2
    /// already fixed for `callees_of`.
    #[test]
    fn filtered_edges_of_checks_each_occurrence_independently_and_filters_by_required_and_forbidden_bits() {
        use crate::graph::reasons;

        let candidates = vec![
            // A -> B: two SEPARATE occurrences, checked independently --
            // the first alone would not satisfy the filter below.
            Candidate::new(1, reasons::SAME_PACKAGE),
            Candidate::new(1, reasons::RECEIVER_TYPE_MATCH),
            // A -> C: one occurrence, carries the MISMATCH bit.
            Candidate::new(2, reasons::RECEIVER_TYPE_MATCH | reasons::RECEIVER_TYPE_MISMATCH),
        ];
        let references = vec![
            Reference { from: 0, file: 1, line: 1, kind: 0, cand_start: 0, cand_len: 1 },
            Reference { from: 0, file: 1, line: 2, kind: 0, cand_start: 1, cand_len: 1 },
            Reference { from: 0, file: 1, line: 3, kind: 0, cand_start: 2, cand_len: 1 },
        ];
        let index = AdjacencyIndex::build_forward(3, &references, &candidates);

        // required RECEIVER_TYPE_MATCH, forbidden RECEIVER_TYPE_MISMATCH:
        // B qualifies (its SECOND occurrence, checked on its own, carries
        // RECEIVER_TYPE_MATCH and nothing else); C is excluded (its one
        // occurrence carries the forbidden bit).
        let mut filtered = index.filtered_edges_of(0, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH);
        filtered.sort_unstable();
        assert_eq!(filtered, vec![1], "B must survive (one occurrence carries RECEIVER_TYPE_MATCH alone); C must be excluded");

        // No filter at all (0, 0) must return every distinct target.
        let mut unfiltered = index.filtered_edges_of(0, 0, 0);
        unfiltered.sort_unstable();
        assert_eq!(unfiltered, vec![1, 2], "an empty required/forbidden mask must exclude nothing");

        // Reverse index has no evidence -- must return empty, never panic
        // or fabricate a result from unpopulated data.
        let reverse = AdjacencyIndex::build_reverse(3, &references, &candidates);
        assert!(reverse.filtered_edges_of(1, 0, 0).is_empty(), "reverse index has no evidence -- must return empty");

        // Out-of-range node must return empty, never panic.
        assert!(index.filtered_edges_of(999, 0, 0).is_empty());
    }

    /// #1924/#1925: the central discriminating test for per-occurrence
    /// filtering. `caller` targets `target` via TWO SEPARATE occurrences:
    /// one GENUINE (`RECEIVER_TYPE_MATCH` alone, as if from a real
    /// `Helper`-typed receiver) and one FABRICATED (`RECEIVER_TYPE_
    /// MISMATCH` alone, as if from an unrelated `String`-typed receiver's
    /// bare-name/arity fallback). `required=RECEIVER_TYPE_MATCH,
    /// forbidden=RECEIVER_TYPE_MISMATCH` must still find `target`: the
    /// genuine occurrence alone satisfies the filter. A merge-then-mask
    /// implementation would compute `MATCH | MISMATCH` for this pair and
    /// wrongly drop it.
    #[test]
    fn filtered_edges_of_applies_the_filter_per_occurrence_not_merged_across_them() {
        use crate::graph::reasons;

        let candidates = vec![
            // Occurrence 1: genuine, MATCH only.
            Candidate::new(1, reasons::RECEIVER_TYPE_MATCH),
            // Occurrence 2: fabricated, MISMATCH only (no MATCH at all).
            Candidate::new(1, reasons::RECEIVER_TYPE_MISMATCH),
        ];
        let references = vec![
            Reference { from: 0, file: 1, line: 1, kind: 0, cand_start: 0, cand_len: 1 },
            Reference { from: 0, file: 1, line: 2, kind: 0, cand_start: 1, cand_len: 1 },
        ];
        let index = AdjacencyIndex::build_forward(2, &references, &candidates);

        let filtered = index.filtered_edges_of(0, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH);
        assert_eq!(
            filtered,
            vec![1],
            "the genuine MATCH-only occurrence must keep this edge, even though a SEPARATE \
             fabricated occurrence of the same (from, to) pair carries the forbidden bit"
        );

        // Sanity: edge_evidence (unfiltered, merge-across-occurrences by
        // design) DOES report both bits together -- confirming the fixture
        // genuinely exercises the merge-vs-per-occurrence distinction.
        assert_eq!(
            index.evidence_for_edge(0, 1),
            Some(reasons::RECEIVER_TYPE_MATCH | reasons::RECEIVER_TYPE_MISMATCH),
            "fixture sanity: edge_evidence's OWN merge-across-occurrences contract is unchanged"
        );
    }

    /// #1924/#1925: `build_reverse_with_evidence` must populate `reasons`
    /// (unlike `build_reverse`) -- NOT `ambiguous`, which has no consumer
    /// on this direction at all (see the sibling `leaves_ambiguous_
    /// unpopulated` test below) -- while `edges`/`offsets` (edge presence)
    /// stay byte-for-byte identical to the evidence-free `build_reverse`.
    #[test]
    fn build_reverse_with_evidence_populates_evidence_while_edges_stay_identical_to_build_reverse() {
        use crate::graph::reasons;

        let (references, candidates) = sample_graph();
        let plain = AdjacencyIndex::build_reverse(4, &references, &candidates);
        let with_evidence = AdjacencyIndex::build_reverse_with_evidence(4, &references, &candidates);

        assert_eq!(plain.edges, with_evidence.edges, "edge presence must be identical regardless of needs_evidence");
        assert_eq!(plain.offsets, with_evidence.offsets);

        assert_eq!(
            plain.evidence_for_edge(1, 0),
            None,
            "the plain reverse index must still report no evidence at all (fail-safe)"
        );
        assert_eq!(
            with_evidence.evidence_for_edge(1, 0),
            Some(0),
            "the evidence-populated reverse index must report the real (possibly empty) bits, not None"
        );

        // filtered_edges_of must also work on this direction now.
        let filtered = with_evidence.filtered_edges_of(3, 0, 0);
        let mut filtered = filtered;
        filtered.sort_unstable();
        assert_eq!(filtered, vec![0, 1], "both A and B must still be found as callers of D via the filtered accessor");
        let _ = reasons::SAME_FILE; // keep the import meaningful if unused elsewhere
    }

    /// #1924/#1925: `build_reverse_with_evidence` populates `reasons` (what
    /// `evidence_for_edge`/`filtered_edges_of` read) but has no consumer
    /// for `ambiguous` at all, so it stays unpopulated -- `ambiguous_for_
    /// edge` must report `None` on this index (via the `has_ambiguous`
    /// flag), never index into the empty array, while `evidence_for_edge`
    /// and `filtered_edges_of` both keep working on the SAME index.
    #[test]
    fn build_reverse_with_evidence_leaves_ambiguous_unpopulated_while_reasons_still_works() {
        let (references, candidates) = sample_graph();
        let with_evidence = AdjacencyIndex::build_reverse_with_evidence(4, &references, &candidates);

        assert_eq!(
            with_evidence.ambiguous_for_edge(1, 0),
            None,
            "ambiguous was never populated on this index -- must report None, never index into an empty Vec"
        );
        assert_eq!(
            with_evidence.evidence_for_edge(1, 0),
            Some(0),
            "reasons WAS populated on this same index -- evidence_for_edge must still work"
        );
        let filtered = with_evidence.filtered_edges_of(3, 0, 0);
        let mut filtered = filtered;
        filtered.sort_unstable();
        assert_eq!(filtered, vec![0, 1], "filtered_edges_of must also still find both A and B as callers of D");
        assert!(with_evidence.ambiguous.is_empty(), "the ambiguous array itself must stay empty -- no wasted allocation");
    }

    /// Bug #1900 (review round 3, coordinator-authorized): MEASURED (not
    /// theoretical) proof of the per-edge byte savings, at a scale cheap
    /// enough to build in a unit test (50,000 edges, one per symbol pair).
    /// `Vec::capacity()` on the private `ambiguous`/`reasons` fields
    /// (accessible here since this test module is `super::*` in the same
    /// file) shows the forward index allocates both fully (one `bool` +
    /// one `u16` per edge, as before), while the reverse index allocates
    /// ZERO capacity for both -- real evidence that `needs_evidence: false`
    /// skips the allocation entirely, not merely leaves it unpopulated.
    #[test]
    fn reverse_index_allocates_zero_capacity_for_evidence_arrays_while_forward_allocates_fully() {
        const EDGE_COUNT: usize = 50_000;
        let mut candidates = Vec::with_capacity(EDGE_COUNT);
        let mut references = Vec::with_capacity(EDGE_COUNT);
        for i in 0..EDGE_COUNT as u32 {
            candidates.push(Candidate::new(i + 1, 0));
            references.push(Reference { from: i, file: 1, line: 1, kind: 0, cand_start: i, cand_len: 1 });
        }

        let forward = AdjacencyIndex::build_forward(EDGE_COUNT + 1, &references, &candidates);
        let reverse = AdjacencyIndex::build_reverse(EDGE_COUNT + 1, &references, &candidates);

        assert_eq!(forward.ambiguous.capacity(), EDGE_COUNT, "forward index must still allocate the full ambiguous array");
        assert_eq!(forward.reasons.capacity(), EDGE_COUNT, "forward index must still allocate the full reasons array");
        assert_eq!(reverse.ambiguous.capacity(), 0, "reverse index must allocate ZERO capacity for ambiguous -- measured, not assumed");
        assert_eq!(reverse.reasons.capacity(), 0, "reverse index must allocate ZERO capacity for reasons -- measured, not assumed");

        // edges/offsets (what callers_of actually reads) are unaffected.
        assert_eq!(reverse.edges.len(), EDGE_COUNT, "edge presence must be identical regardless of needs_evidence");
    }
}
