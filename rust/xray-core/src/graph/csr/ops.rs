//! Engine-provided BOUNDED graph query ops (Story #1787, S2, AC7).
//!
//! These exist so a user-authored `analyze_graph` callback never has to
//! write its own traversal loop: every op here takes an explicit
//! `max_depth` (where applicable) and terminates by construction --
//! `visited`-set membership bounds total work to the graph's own finite
//! symbol/edge count, never an unbounded caller-controlled loop (Rule 14,
//! anti-unbounded-loop). A caller who only ever calls these primitives
//! cannot make `analyze_graph` diverge; the OS-level process
//! timeout/kill in `crate::graph::analyze::process` is the backstop for a
//! callback that writes its own unbounded loop OUTSIDE these ops.
//!
//! All ops read the "call graph" direction off `Reference.from` -- the
//! DENSE id of the symbol the reference site is textually INSIDE (see
//! `crate::graph::bind::resolve::enclosing_symbol`) -- and each
//! `Reference`'s candidate window as its (possibly ambiguous) targets.
//! `callees_of(x)` therefore means "every candidate target of every
//! reference written inside x"; `callers_of(y)` means "every symbol whose
//! own reference proposed y as a candidate", regardless of whether that
//! candidate was the reference's only one.

use super::code_graph::CodeGraph;
use std::collections::{HashSet, VecDeque};

#[cfg(test)]
thread_local! {
    /// Dual-review defect M2 test seam (mirrors the `#[cfg(test)]`-only
    /// `CANDIDATE_CAPACITY_ALLOCATIONS` counter in `csr::builder`): counts
    /// how many `Reference` entries `callees_of` compares against per
    /// call, so a test can assert the ACTUAL work performed scales with
    /// the graph's edge count -- not with (nodes visited x total edges),
    /// which is what the pre-fix linear scan produces. THREAD-LOCAL,
    /// deliberately -- see `CANDIDATE_CAPACITY_ALLOCATIONS`'s own doc
    /// comment for why (parallel `cargo test` execution across this
    /// crate). Compiled out entirely in non-test builds.
    pub(crate) static REFERENCE_COMPARISON_COUNT: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

#[cfg(test)]
pub(crate) fn reset_reference_comparison_count() {
    REFERENCE_COMPARISON_COUNT.with(|count| count.set(0));
}

#[cfg(test)]
pub(crate) fn reference_comparison_count() -> usize {
    REFERENCE_COMPARISON_COUNT.with(|count| count.get())
}

impl CodeGraph {
    /// Bounded BFS over `callees_of` edges starting at every id in `roots`
    /// (roots count as depth 0). Terminates by construction: `visited`
    /// admits each dense id at most once -- roots included, so a caller
    /// passing duplicate root ids still gets each node exactly once -- so
    /// total work is bounded by `min(reachable set size, depth-limited
    /// frontier)`; there is no way for this loop to run more than once
    /// per distinct node the graph actually contains, cycle or not
    /// (Rule 14).
    pub fn reachable_from(&self, roots: &[u32], max_depth: usize) -> Vec<u32> {
        let mut visited: HashSet<u32> = HashSet::new();
        let mut queue: VecDeque<(u32, usize)> = VecDeque::new();
        for &root in roots {
            if visited.insert(root) {
                queue.push_back((root, 0));
            }
        }
        let mut order = Vec::new();
        while let Some((node, depth)) = queue.pop_front() {
            order.push(node);
            if depth >= max_depth {
                continue;
            }
            for callee in self.callees_of(node) {
                if visited.insert(callee) {
                    queue.push_back((callee, depth + 1));
                }
            }
        }
        order
    }
    /// Every candidate target (dense symbol id) of every reference written
    /// textually inside `dense_symbol_id`. M2 fix: O(out-degree) via the
    /// precomputed CSR forward adjacency index (`callees_index`, built
    /// ONCE at graph construction) -- NEVER a re-scan of every reference
    /// in the repository, which made this (and every caller that queries
    /// it once per node: `reachable_from`, `shortest_path_to_any`,
    /// `strongly_connected_components`) O(V*E) before this fix.
    pub fn callees_of(&self, dense_symbol_id: u32) -> Vec<u32> {
        self.callees_index(dense_symbol_id).to_vec()
    }

    /// Every symbol (dense id) that has at least one reference proposing
    /// `dense_symbol_id` as a candidate target -- the reverse of
    /// `callees_of`. M2 fix: O(in-degree) via the precomputed CSR reverse
    /// adjacency index, same rationale as `callees_of` above.
    pub fn callers_of(&self, dense_symbol_id: u32) -> Vec<u32> {
        self.callers_index(dense_symbol_id).to_vec()
    }

    /// Bounded BFS from `from` to the nearest node in `targets` (shortest
    /// hop count wins), stopping the frontier once `max_depth` is
    /// exceeded. `from` itself counts as depth 0, so `targets` containing
    /// `from` returns the trivial single-node path immediately. Same
    /// visited-set termination bound as `reachable_from` (Rule 14): each
    /// dense id is enqueued at most once.
    pub fn shortest_path_to_any(&self, from: u32, targets: &[u32], max_depth: usize) -> Option<Vec<u32>> {
        if targets.contains(&from) {
            return Some(vec![from]);
        }
        let mut visited: HashSet<u32> = HashSet::from([from]);
        let mut parent: std::collections::HashMap<u32, u32> = std::collections::HashMap::new();
        let mut queue: VecDeque<(u32, usize)> = VecDeque::from([(from, 0)]);
        while let Some((node, depth)) = queue.pop_front() {
            if depth >= max_depth {
                continue;
            }
            for callee in self.callees_of(node) {
                if !visited.insert(callee) {
                    continue;
                }
                parent.insert(callee, node);
                if targets.contains(&callee) {
                    return Some(reconstruct_path(&parent, from, callee));
                }
                queue.push_back((callee, depth + 1));
            }
        }
        None
    }
}

/// Walks `parent` pointers backward from `end` to `start` (inclusive of
/// both), then reverses -- `parent` is finite (one entry per node the BFS
/// above ever visited), so this walk terminates in at most that many
/// steps (Rule 14).
fn reconstruct_path(parent: &std::collections::HashMap<u32, u32>, start: u32, end: u32) -> Vec<u32> {
    let mut path = vec![end];
    let mut current = end;
    while current != start {
        current = *parent
            .get(&current)
            .expect("every non-start node on the path must have a recorded parent");
        path.push(current);
    }
    path.reverse();
    path
}

impl CodeGraph {
    /// Every strongly-connected component of the whole graph (Tarjan's
    /// algorithm), over ALL interned symbols -- including one with zero
    /// references, which forms its own singleton component. Implemented
    /// ITERATIVELY (`TarjanContext::run_from` uses an explicit `work`
    /// stack, never native recursion): this codebase already hit a real
    /// SIGABRT from unbounded native recursion over user-controlled depth
    /// (Bug #1795, `owned_node.rs`/PREAMBLE's `has_descendant_of_kind`)
    /// and deliberately never repeats that shape here. Every node is
    /// pushed onto `work` at most once (guarded by `index[..].is_some()`),
    /// so total work is O(V+E) and provably finite (Rule 14).
    pub fn strongly_connected_components(&self) -> Vec<Vec<u32>> {
        let n = self.symbol_count();
        let adjacency: Vec<Vec<u32>> = (0..n as u32).map(|v| self.callees_of(v)).collect();
        let mut ctx = TarjanContext::new(n);
        for start in 0..n as u32 {
            if ctx.index[start as usize].is_none() {
                ctx.run_from(start, &adjacency);
            }
        }
        ctx.components
    }
}

/// Mutable state for the iterative Tarjan SCC walk used by
/// `CodeGraph::strongly_connected_components` above -- pulled into its own
/// struct so that method itself stays small and Tarjan's standard
/// `index`/`lowlink`/`on_stack`/`stack` fields plus the accumulated
/// `components` output are named once instead of threaded through as five
/// separate local variables.
struct TarjanContext {
    index: Vec<Option<u32>>,
    low: Vec<u32>,
    on_stack: Vec<bool>,
    stack: Vec<u32>,
    next_index: u32,
    components: Vec<Vec<u32>>,
}

impl TarjanContext {
    fn new(n: usize) -> Self {
        TarjanContext {
            index: vec![None; n],
            low: vec![0; n],
            on_stack: vec![false; n],
            stack: Vec::new(),
            next_index: 0,
            components: Vec::new(),
        }
    }

    /// Runs one iterative Tarjan DFS rooted at `start`. `work` holds
    /// `(node, next_child_position)` frames standing in for the call
    /// stack; it grows by exactly one frame per NEWLY discovered node
    /// (bounded by the graph's total symbol count) and shrinks by one
    /// every time a node's full adjacency list has been examined, so this
    /// terminates in a finite number of steps regardless of the graph's
    /// edge structure (cycles included -- `index[..].is_some()` prevents
    /// re-discovering a node, which is what makes a cycle safe here
    /// rather than an infinite recursion).
    fn run_from(&mut self, start: u32, adjacency: &[Vec<u32>]) {
        let discover = |ctx: &mut TarjanContext, node: u32| {
            ctx.index[node as usize] = Some(ctx.next_index);
            ctx.low[node as usize] = ctx.next_index;
            ctx.next_index += 1;
            ctx.stack.push(node);
            ctx.on_stack[node as usize] = true;
        };
        discover(self, start);
        let mut work: Vec<(u32, usize)> = vec![(start, 0)];

        while let Some(&mut (v, ref mut pos)) = work.last_mut() {
            if *pos < adjacency[v as usize].len() {
                let w = adjacency[v as usize][*pos];
                *pos += 1;
                if self.index[w as usize].is_none() {
                    discover(self, w);
                    work.push((w, 0));
                } else if self.on_stack[w as usize] {
                    self.low[v as usize] = self.low[v as usize].min(self.index[w as usize].unwrap());
                }
            } else {
                work.pop();
                if let Some(&(parent, _)) = work.last() {
                    self.low[parent as usize] = self.low[parent as usize].min(self.low[v as usize]);
                }
                if self.low[v as usize] == self.index[v as usize].unwrap() {
                    // Pop the completed component rooted at `v` off the
                    // stack -- bounded by `self.stack.len()` (finite, at
                    // most the graph's total symbol count).
                    let mut component = Vec::new();
                    loop {
                        let w = self.stack.pop().expect("root's own component must still be on the stack");
                        self.on_stack[w as usize] = false;
                        component.push(w);
                        if w == v {
                            break;
                        }
                    }
                    self.components.push(component);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::builder::CodeGraphBuilder;
    use super::super::candidate::Candidate;
    use super::{reference_comparison_count, reset_reference_comparison_count};
    use crate::graph::identity::make_symbol_id;
    use crate::graph::reasons;

    /// AC7: "engine-provided BOUNDED graph ops ... callees_of". A is the
    /// enclosing symbol of two references: one targeting B, one targeting
    /// C (ambiguous with D). `callees_of(A)` must return every candidate
    /// across BOTH references, and must return nothing for a symbol with
    /// zero outgoing references (D).
    #[test]
    fn callees_of_returns_targets_of_every_reference_whose_from_matches() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(1, 1));
        let c = builder.intern_symbol(make_symbol_id(2, 0));
        let d = builder.intern_symbol(make_symbol_id(2, 1));

        builder.add_reference(a, 1, 10, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        builder.add_reference(
            a,
            1,
            11,
            0,
            &[Candidate::new(c, reasons::SAME_PACKAGE), Candidate::new(d, reasons::SAME_PACKAGE)],
        );
        // An unrelated reference whose `from` is B, not A -- must not leak
        // into A's callee set.
        builder.add_reference(b, 1, 12, 0, &[Candidate::new(d, reasons::SAME_FILE)]);

        let graph = builder.build();

        let mut callees = graph.callees_of(a);
        callees.sort_unstable();
        let mut expected = vec![b, c, d];
        expected.sort_unstable();
        assert_eq!(callees, expected);

        assert!(graph.callees_of(d).is_empty(), "D has no outgoing references");
    }

    /// AC7: "callers_of" is the reverse of "callees_of" -- every symbol
    /// with a reference that PROPOSED the target as a candidate, even when
    /// that candidate was one of several ambiguous options on its own
    /// reference (C below is proposed alongside D; both A and B must still
    /// show up as callers of C).
    #[test]
    fn callers_of_returns_every_symbol_whose_reference_proposed_the_target_as_a_candidate() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(1, 1));
        let c = builder.intern_symbol(make_symbol_id(2, 0));
        let d = builder.intern_symbol(make_symbol_id(2, 1));

        builder.add_reference(a, 1, 10, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
        builder.add_reference(
            b,
            1,
            11,
            0,
            &[Candidate::new(c, reasons::SAME_PACKAGE), Candidate::new(d, reasons::SAME_PACKAGE)],
        );

        let graph = builder.build();

        let mut callers_of_c = graph.callers_of(c);
        callers_of_c.sort_unstable();
        let mut expected = vec![a, b];
        expected.sort_unstable();
        assert_eq!(callers_of_c, expected);

        assert_eq!(graph.callers_of(d), vec![b]);
        assert!(graph.callers_of(a).is_empty(), "nothing proposes A as a candidate");
    }

    /// AC7: "reachable_from(roots, max_depth)" -- a bounded BFS over
    /// `callees_of` edges. Diamond shape: A -> B -> D and A -> C -> D, plus
    /// a back-edge D -> A closing a CYCLE. A wrong implementation with no
    /// visited-set would either loop forever on the D -> A back-edge or
    /// report D twice (once via B, once via C) -- this fixture makes BOTH
    /// failure modes observable: exactly one occurrence of every node, and
    /// max_depth still cuts off E (3 hops from A along the shortest path).
    #[test]
    fn reachable_from_stops_at_max_depth_and_never_visits_a_node_twice() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(6);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(1, 1));
        let c = builder.intern_symbol(make_symbol_id(1, 2));
        let d = builder.intern_symbol(make_symbol_id(1, 3));
        let e = builder.intern_symbol(make_symbol_id(1, 4));

        builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        builder.add_reference(a, 1, 2, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
        builder.add_reference(b, 1, 3, 0, &[Candidate::new(d, reasons::SAME_FILE)]);
        builder.add_reference(c, 1, 4, 0, &[Candidate::new(d, reasons::SAME_FILE)]);
        builder.add_reference(d, 1, 5, 0, &[Candidate::new(a, reasons::SAME_FILE)]); // cycle back to A
        builder.add_reference(d, 1, 6, 0, &[Candidate::new(e, reasons::SAME_FILE)]);

        let graph = builder.build();

        let reached = graph.reachable_from(&[a], 100);
        let mut sorted = reached.clone();
        sorted.sort_unstable();
        assert_eq!(sorted, vec![a, b, c, d, e], "every node reachable, each exactly once");
        for node in [a, b, c, d, e] {
            assert_eq!(
                reached.iter().filter(|&&n| n == node).count(),
                1,
                "node {node} must appear exactly once despite being reachable via two paths / a cycle"
            );
        }

        // E is 3 hops from A along the shortest path (A -> B -> D -> E);
        // max_depth=2 must exclude it while still including the diamond.
        let mut bounded = graph.reachable_from(&[a], 2);
        bounded.sort_unstable();
        assert_eq!(bounded, vec![a, b, c, d], "E is 3 hops away, beyond max_depth=2");
    }

    /// AC7: "shortest_path_to_any(from, targets, max_depth)". Diamond
    /// A -> B -> D and A -> C -> D: the shortest path from A to {D} must
    /// be exactly 2 hops (via B or C, either is fine -- length is what's
    /// asserted, not which branch), never the accidentally-longer path a
    /// DFS-without-shortest-tracking implementation might return.
    #[test]
    fn shortest_path_to_any_finds_the_minimum_hop_path_and_none_beyond_max_depth() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(4);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(1, 1));
        let c = builder.intern_symbol(make_symbol_id(1, 2));
        let d = builder.intern_symbol(make_symbol_id(1, 3));

        builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        builder.add_reference(a, 1, 2, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
        builder.add_reference(b, 1, 3, 0, &[Candidate::new(d, reasons::SAME_FILE)]);
        builder.add_reference(c, 1, 4, 0, &[Candidate::new(d, reasons::SAME_FILE)]);

        let graph = builder.build();

        let path = graph.shortest_path_to_any(a, &[d], 100).expect("D is reachable from A");
        assert_eq!(path.len(), 3, "shortest path A -> {{B or C}} -> D has exactly 3 nodes");
        assert_eq!(path.first(), Some(&a));
        assert_eq!(path.last(), Some(&d));

        assert_eq!(
            graph.shortest_path_to_any(a, &[d], 1),
            None,
            "D is 2 hops away, beyond max_depth=1"
        );
        assert_eq!(
            graph.shortest_path_to_any(a, &[a], 0),
            Some(vec![a]),
            "a target that IS the start node is a trivial zero-hop path"
        );
    }

    /// AC7: "strongly_connected_components". A three-cycle A -> B -> C ->
    /// A forms ONE component; a standalone symbol D (no edges at all) and
    /// a one-way pair E -> F (no back edge) form THREE separate
    /// singleton components. A wrong implementation that merged reachable
    /// nodes regardless of MUTUAL reachability (e.g. plain weakly-
    /// connected components) would incorrectly merge E and F.
    #[test]
    fn strongly_connected_components_groups_only_mutually_reachable_nodes() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(5);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(1, 1));
        let c = builder.intern_symbol(make_symbol_id(1, 2));
        let d = builder.intern_symbol(make_symbol_id(1, 3));
        let e = builder.intern_symbol(make_symbol_id(1, 4));
        let f = builder.intern_symbol(make_symbol_id(1, 5));

        builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        builder.add_reference(b, 1, 2, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
        builder.add_reference(c, 1, 3, 0, &[Candidate::new(a, reasons::SAME_FILE)]);
        builder.add_reference(e, 1, 4, 0, &[Candidate::new(f, reasons::SAME_FILE)]);
        // D has zero references at all -- still must be interned (via
        // intern_symbol above) and still must appear as its own component.
        let _ = d;

        let graph = builder.build();
        let components = graph.strongly_connected_components();

        let find_component_containing = |node: u32| -> Vec<u32> {
            let mut comp = components
                .iter()
                .find(|comp| comp.contains(&node))
                .unwrap_or_else(|| panic!("node {node} must belong to exactly one component"))
                .clone();
            comp.sort_unstable();
            comp
        };

        let mut abc = vec![a, b, c];
        abc.sort_unstable();
        assert_eq!(find_component_containing(a), abc);
        assert_eq!(find_component_containing(b), abc);
        assert_eq!(find_component_containing(c), abc);

        assert_eq!(find_component_containing(d), vec![d]);
        assert_eq!(find_component_containing(e), vec![e], "E and F must NOT merge -- no back-edge from F to E");
        assert_eq!(find_component_containing(f), vec![f]);

        let total_nodes: usize = components.iter().map(|c| c.len()).sum();
        assert_eq!(total_nodes, graph.symbol_count(), "every interned symbol appears in exactly one component");
    }

    /// Dual-review defect M2: at the amendment's own Elasticsearch-scale
    /// figures (~215K declarations, ~834K call sites) a single
    /// `strongly_connected_components()` call was estimated at roughly
    /// 1.8e11 reference comparisons, because `callees_of` scans EVERY
    /// reference in the repository per call and SCC calls it once per
    /// node -- O(V*E), not O(V+E).
    ///
    /// This test builds a graph small enough to run instantly either way
    /// (so it never becomes a timeout-flaky wall-clock test) but PROVES
    /// the complexity class directly: N symbols in one big cycle (N
    /// references, one outgoing edge each) means the true O(V+E) work is
    /// ~2N reference-field reads, while the O(V*E) pre-fix behavior does
    /// N calls to callees_of, each scanning all N references -- N^2
    /// comparisons. Asserting on the REFERENCE_COMPARISON_COUNT work
    /// counter (not wall-clock, which is load-dependent) discriminates
    /// the two unambiguously: N^2 (90,000 for N=300) fails a `<= 2*N`
    /// bound that O(V+E) trivially satisfies.
    #[test]
    fn strongly_connected_components_does_not_scan_every_reference_per_node() {
        const N: usize = 300;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(N);
        let symbols: Vec<u32> = (0..N).map(|i| builder.intern_symbol(make_symbol_id(i as u32, 0))).collect();
        for i in 0..N {
            let target = symbols[(i + 1) % N];
            builder.add_reference(symbols[i], 1, i as u32, 0, &[Candidate::new(target, reasons::SAME_FILE)]);
        }
        let graph = builder.build();

        reset_reference_comparison_count();
        let components = graph.strongly_connected_components();

        // Sanity: still functionally correct -- one big cycle is one SCC.
        assert_eq!(components.len(), 1, "one big cycle must form exactly one strongly-connected component");
        assert_eq!(components[0].len(), N);

        let comparisons = reference_comparison_count();
        assert!(
            comparisons <= 2 * N,
            "strongly_connected_components() performed {comparisons} reference comparisons for a \
             {N}-node/{N}-edge graph -- expected O(V+E) (<= {}), got O(V*E)-shaped work \
             (dual-review defect M2: callees_of must not scan every reference per call)",
            2 * N
        );
    }
}
