//! Evidence-filtered counterparts to `super::ops`'s bounded traversal
//! primitives (#1924/#1925, epic #1906): `reachable_from_filtered`/
//! `reachable_to_filtered`/`strongly_connected_components_filtered`. An
//! edge qualifies when `(bits & required_bits) == required_bits && (bits &
//! forbidden_bits) == 0` -- `CodeGraph::callees_of_filtered`/`callers_of_
//! filtered`'s own contract. `required_bits: 0, forbidden_bits: 0`
//! excludes nothing, so every filtered primitive here then behaves
//! identically to its unfiltered sibling.
//!
//! `reachable_from_filtered`/`reachable_to_filtered` share ONE bounded-BFS
//! implementation (`bounded_bfs_filtered`, closure-parameterized over the
//! per-node edge source) rather than two independently-maintained copies
//! of the same loop (Rule 4, anti-duplication) -- the two differ only in
//! which direction feeds it (`callees_of_filtered` vs `callers_of_
//! filtered`). `strongly_connected_components_filtered` similarly REUSES
//! `super::ops::strongly_connected_components_over_adjacency` directly:
//! the Tarjan walk itself never differs between the unfiltered and
//! filtered primitives, only how the adjacency list feeding it was built.
//!
//! This is what lets an evaluator (via `GraphHandle`) analyse the "strong
//! subgraph" -- e.g. requiring `RECEIVER_TYPE_MATCH` and forbidding
//! `RECEIVER_TYPE_MISMATCH` to drop #1924/#1925's fabricated receiver
//! edges from its own reachability/SCC analysis -- without this binder
//! ever deleting them from the raw graph (`callees_of`/`callers_of`/
//! `strongly_connected_components`, all unchanged and untouched by this
//! module).

use super::code_graph::CodeGraph;
use super::ops::strongly_connected_components_over_adjacency;
use std::collections::{HashSet, VecDeque};

/// Shared bounded-BFS core for `reachable_from_filtered`/`reachable_to_
/// filtered`: `starts` count as depth 0 (root-inclusive), `edges_of(node)`
/// supplies the (already evidence-filtered) next hops for `node`, and
/// `visited` bounds total work to at most one enqueue per distinct node
/// (Rule 14, anti-unbounded-loop) -- identical termination guarantee to
/// `super::ops`'s own `reachable_from`/`reachable_to`.
fn bounded_bfs_filtered<F>(starts: &[u32], max_depth: usize, mut edges_of: F) -> Vec<u32>
where
    F: FnMut(u32) -> Vec<u32>,
{
    let mut visited: HashSet<u32> = HashSet::new();
    let mut queue: VecDeque<(u32, usize)> = VecDeque::new();
    for &start in starts {
        if visited.insert(start) {
            queue.push_back((start, 0));
        }
    }
    let mut order = Vec::new();
    while let Some((node, depth)) = queue.pop_front() {
        order.push(node);
        if depth >= max_depth {
            continue;
        }
        for next in edges_of(node) {
            if visited.insert(next) {
                queue.push_back((next, depth + 1));
            }
        }
    }
    order
}

impl CodeGraph {
    /// The evidence-filtered counterpart of `reachable_from` -- bounded
    /// BFS over `callees_of_filtered` edges instead of `callees_of`.
    pub fn reachable_from_filtered(&self, roots: &[u32], max_depth: usize, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        bounded_bfs_filtered(roots, max_depth, |node| self.callees_of_filtered(node, required_bits, forbidden_bits))
    }

    /// The evidence-filtered counterpart of `reachable_to` -- bounded BFS
    /// over `callers_of_filtered` edges instead of `callers_of`.
    pub fn reachable_to_filtered(&self, targets: &[u32], max_depth: usize, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        bounded_bfs_filtered(targets, max_depth, |node| self.callers_of_filtered(node, required_bits, forbidden_bits))
    }

    /// The evidence-filtered counterpart of `strongly_connected_
    /// components` -- Tarjan's algorithm over a filtered adjacency list
    /// (`callees_of_filtered` per node) instead of the unfiltered one.
    /// Reuses `super::ops::strongly_connected_components_over_adjacency`
    /// directly rather than a second copy of the iterative Tarjan walk.
    pub fn strongly_connected_components_filtered(&self, required_bits: u16, forbidden_bits: u16) -> Vec<Vec<u32>> {
        let n = self.symbol_count();
        let adjacency: Vec<Vec<u32>> =
            (0..n as u32).map(|v| self.callees_of_filtered(v, required_bits, forbidden_bits)).collect();
        strongly_connected_components_over_adjacency(n, &adjacency)
    }
}

#[cfg(test)]
mod tests {
    use super::super::builder::CodeGraphBuilder;
    use super::super::candidate::Candidate;
    use crate::graph::identity::make_symbol_id;
    use crate::graph::reasons;

    /// Mirrors `ops::tests::reachable_from_stops_at_max_depth_and_never_
    /// visits_a_node_twice`'s diamond-plus-cycle fixture (A->B, A->C,
    /// B->D, C->D, D->A cycle, D->E), but every edge is tagged
    /// `RECEIVER_TYPE_MATCH` EXCEPT the `D -> E` edge, which additionally
    /// carries `RECEIVER_TYPE_MISMATCH`. Filtering on
    /// `required=RECEIVER_TYPE_MATCH, forbidden=RECEIVER_TYPE_MISMATCH`
    /// must reach everything the unfiltered traversal reaches EXCEPT `E`.
    #[test]
    fn reachable_from_filtered_excludes_nodes_reachable_only_through_a_forbidden_edge() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(6);
        let a = builder.intern_symbol(make_symbol_id(30, 0));
        let b = builder.intern_symbol(make_symbol_id(30, 1));
        let c = builder.intern_symbol(make_symbol_id(30, 2));
        let d = builder.intern_symbol(make_symbol_id(30, 3));
        let e = builder.intern_symbol(make_symbol_id(30, 4));

        builder.add_reference(a, 30, 1, 0, &[Candidate::new(b, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(a, 30, 2, 0, &[Candidate::new(c, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(b, 30, 3, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(c, 30, 4, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(d, 30, 5, 0, &[Candidate::new(a, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(
            d,
            30,
            6,
            0,
            &[Candidate::new(e, reasons::RECEIVER_TYPE_MATCH | reasons::RECEIVER_TYPE_MISMATCH)],
        );
        let graph = builder.build();

        let mut unfiltered = graph.reachable_from(&[a], 100);
        unfiltered.sort_unstable();
        assert_eq!(unfiltered, vec![a, b, c, d, e], "fixture sanity: unfiltered reaches everything, E included");

        let mut filtered =
            graph.reachable_from_filtered(&[a], 100, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH);
        filtered.sort_unstable();
        assert_eq!(filtered, vec![a, b, c, d], "E is reachable only through the forbidden-bit edge -- must be excluded");

        // An empty mask must reproduce the unfiltered result exactly.
        let mut everything = graph.reachable_from_filtered(&[a], 100, 0, 0);
        everything.sort_unstable();
        assert_eq!(everything, unfiltered, "an empty required/forbidden mask must exclude nothing");
    }

    /// Mirrors the same fixture from the CALLERS direction, matching
    /// `ops::tests::reachable_to_mirrors_reachable_from_over_the_reverse_
    /// adjacency`: querying `reachable_to_filtered(&[e], ..)` must find
    /// nothing but `E` itself, since `D -> E` is the only edge into `E`
    /// and it carries the forbidden bit.
    #[test]
    fn reachable_to_filtered_excludes_callers_reachable_only_through_a_forbidden_edge() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(6);
        let a = builder.intern_symbol(make_symbol_id(31, 0));
        let b = builder.intern_symbol(make_symbol_id(31, 1));
        let c = builder.intern_symbol(make_symbol_id(31, 2));
        let d = builder.intern_symbol(make_symbol_id(31, 3));
        let e = builder.intern_symbol(make_symbol_id(31, 4));

        builder.add_reference(a, 31, 1, 0, &[Candidate::new(b, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(a, 31, 2, 0, &[Candidate::new(c, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(b, 31, 3, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(c, 31, 4, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(d, 31, 5, 0, &[Candidate::new(a, reasons::RECEIVER_TYPE_MATCH)]);
        builder.add_reference(
            d,
            31,
            6,
            0,
            &[Candidate::new(e, reasons::RECEIVER_TYPE_MATCH | reasons::RECEIVER_TYPE_MISMATCH)],
        );
        let graph = builder.build();

        let mut unfiltered = graph.reachable_to(&[e], 100);
        unfiltered.sort_unstable();
        assert_eq!(unfiltered, vec![a, b, c, d, e], "fixture sanity: unfiltered reaches every transitive caller of E");

        let filtered =
            graph.reachable_to_filtered(&[e], 100, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH);
        assert_eq!(filtered, vec![e], "E's only inbound edge carries the forbidden bit -- no caller must survive the filter");
    }

    /// #1924's own acceptance criterion, at the primitive level: a fixture
    /// where a `RECEIVER_TYPE_MISMATCH`-tagged edge closes a FALSE 2-node
    /// cycle (`A.equals -> B.equals`, `B.equals -> A.equals`, exactly the
    /// shape a String receiver's fabricated `.equals()` bare-name/arity
    /// binding produces) must still show that cycle in the UNFILTERED
    /// `strongly_connected_components`, but `strongly_connected_
    /// components_filtered` (forbidding `RECEIVER_TYPE_MISMATCH`) must
    /// return no such cycle -- A and B must each be their own singleton
    /// component.
    #[test]
    fn strongly_connected_components_filtered_drops_a_false_cycle_formed_by_mismatched_receiver_edges() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
        let a = builder.intern_symbol(make_symbol_id(32, 0));
        let b = builder.intern_symbol(make_symbol_id(32, 1));
        builder.add_reference(a, 32, 1, 0, &[Candidate::new(b, reasons::RECEIVER_TYPE_MISMATCH)]);
        builder.add_reference(b, 32, 2, 0, &[Candidate::new(a, reasons::RECEIVER_TYPE_MISMATCH)]);
        let graph = builder.build();

        let unfiltered = graph.strongly_connected_components();
        let false_cycle = unfiltered
            .iter()
            .find(|component| component.contains(&a))
            .expect("A must belong to some component");
        assert!(false_cycle.contains(&b), "fixture sanity: the unfiltered graph must show the false 2-node cycle");
        assert_eq!(false_cycle.len(), 2, "fixture sanity: the false cycle has exactly 2 members");

        let filtered = graph.strongly_connected_components_filtered(0, reasons::RECEIVER_TYPE_MISMATCH);
        let a_component = filtered
            .iter()
            .find(|component| component.contains(&a))
            .expect("A must still belong to some component");
        assert!(!a_component.contains(&b), "forbidding RECEIVER_TYPE_MISMATCH must break the false cycle");
        assert_eq!(a_component.len(), 1, "A must be its own singleton component once the mismatched edge is excluded");

        let b_component = filtered
            .iter()
            .find(|component| component.contains(&b))
            .expect("B must still belong to some component");
        assert_eq!(b_component.len(), 1, "B must likewise be its own singleton component");
    }

    /// An empty required/forbidden mask must reproduce
    /// `strongly_connected_components`'s exact result -- proving the
    /// filtered primitive is not silently a different (e.g. always-empty)
    /// algorithm when nothing is actually filtered.
    #[test]
    fn strongly_connected_components_filtered_with_an_empty_mask_matches_the_unfiltered_result() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
        let a = builder.intern_symbol(make_symbol_id(33, 0));
        let b = builder.intern_symbol(make_symbol_id(33, 1));
        let c = builder.intern_symbol(make_symbol_id(33, 2));
        builder.add_reference(a, 33, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        builder.add_reference(b, 33, 2, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
        builder.add_reference(c, 33, 3, 0, &[Candidate::new(a, reasons::SAME_FILE)]);
        let graph = builder.build();

        let mut unfiltered = graph.strongly_connected_components();
        let mut filtered = graph.strongly_connected_components_filtered(0, 0);
        for component in unfiltered.iter_mut().chain(filtered.iter_mut()) {
            component.sort_unstable();
        }
        unfiltered.sort();
        filtered.sort();
        assert_eq!(filtered, unfiltered, "an empty required/forbidden mask must reproduce the unfiltered SCC result");
    }
}
