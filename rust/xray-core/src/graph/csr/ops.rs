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
//! DENSE id of the symbol the reference site is textually INSIDE. Issue
//! #1930: this is the extractor's own real, AST-walk-tracked enclosing
//! method when one is known and trustworthy (see
//! `crate::graph::bind::resolve::enclosing_symbol_for_site`), falling
//! back to a nearest-preceding-declaration-by-line heuristic
//! (`crate::graph::bind::resolve::enclosing_symbol`) only for a site
//! with no such method (a field initializer, a class-level type
//! reference) or one whose `enclosing_method` is a synthetic scope
//! symbol with no real `Declaration` (a static/instance initializer
//! block, a record compact constructor, a Kotlin getter/setter/`init`
//! block) -- and each `Reference`'s candidate window as its (possibly
//! ambiguous) targets.
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

/// Bug #1929 item 2: returns `raw` with every duplicate removed, keeping
/// each element's FIRST occurrence position -- the same dedup contract
/// `AdjacencyIndex::filtered_edges_of` already documents for its own
/// evidence-filtered callee/caller view ("EXACTLY ONCE, in FIRST-SEEN CSR
/// order, never a `HashMap`'s unspecified iteration order"). `callees_of`/
/// `callers_of` now share this exact behavior instead of diverging from
/// it, so a `required: 0, forbidden: 0` filtered call and its unfiltered
/// counterpart return the identical (deduplicated) target SET. O(raw.len())
/// time and one `HashSet<u32>` sized to at most `raw.len()` -- bounded by
/// this node's own out-/in-degree, never the whole graph (Rule 14).
///
/// Bug #1929 rework item 6 (measured, judgement call): this per-call
/// `HashSet` allocation is genuinely NOT free. Measured on a synthetic
/// 50,000-node/400,000-edge graph (this crate's own documented target
/// scale is ~215K declarations/~834K call sites, see `adjacency.rs`), a
/// full whole-graph `callees_of` pass (one call per node -- the exact
/// pattern `strongly_connected_components`/`reachable_from`/
/// `reachable_to` all use) took ~13ms in `--release`: about 152x the
/// ~86us the raw undeduped `callees_index` accessor takes for the
/// identical pass.
///
/// Deliberately NOT moved to CSR construction time. The SAME shared
/// adjacency edge array also backs `filtered_edges_of`,
/// `ambiguous_for_edge`, and `evidence_for_edge`, each of which needs
/// the RAW, UNDEDUPED, per-occurrence data for its own evidence
/// semantics -- `filtered_edges_of`'s own doc comment: "checked per
/// occurrence, never merged across occurrences ... a genuine edge is
/// never dropped just because a separate, weaker occurrence of the same
/// pair exists". Deduping that shared array at construction time would
/// silently break those three accessors. Doing it correctly would
/// require a SECOND, parallel deduped index used only by `callees_of`/
/// `callers_of`, doubling this crate's adjacency memory footprint for a
/// cost (tens of milliseconds, at most a handful of whole-graph passes
/// per `analyze_graph` invocation) that is not the dominant cost in a
/// real run (parsing/extraction/binding a large repo runs into
/// seconds-to-minutes). Revisit if profiling ever shows this dominating
/// a real `analyze_graph` invocation's wall time.
fn dedup_preserving_first_seen_order(raw: &[u32]) -> Vec<u32> {
    let mut seen: HashSet<u32> = HashSet::with_capacity(raw.len());
    let mut ordered = Vec::with_capacity(raw.len());
    for &id in raw {
        if seen.insert(id) {
            ordered.push(id);
        }
    }
    ordered
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

    /// Bug #1901: the CALLERS-direction counterpart of `reachable_from` --
    /// "how much of the codebase can a change to `targets` affect" is the
    /// transitive CALLERS closure, the opposite of `reachable_from`'s
    /// transitive CALLEES closure. Before this, an evaluator answering that
    /// question had to hand-roll its own BFS over repeated `callers_of`
    /// calls; this is the same bounded traversal `reachable_from` already
    /// proves (root-inclusive, monotonic, convergent, Rule 14), just walked
    /// over the REVERSE CSR adjacency (`callers_of`) instead of the forward
    /// one (`callees_of`). Byte-for-byte identical shape to `reachable_from`
    /// on purpose -- the two are meant to read as mirror images of each
    /// other, never two independently-evolving traversal implementations.
    pub fn reachable_to(&self, targets: &[u32], max_depth: usize) -> Vec<u32> {
        let mut visited: HashSet<u32> = HashSet::new();
        let mut queue: VecDeque<(u32, usize)> = VecDeque::new();
        for &target in targets {
            if visited.insert(target) {
                queue.push_back((target, 0));
            }
        }
        let mut order = Vec::new();
        while let Some((node, depth)) = queue.pop_front() {
            order.push(node);
            if depth >= max_depth {
                continue;
            }
            for caller in self.callers_of(node) {
                if visited.insert(caller) {
                    queue.push_back((caller, depth + 1));
                }
            }
        }
        order
    }

    /// Every candidate target (dense symbol id) of every reference written
    /// textually inside `dense_symbol_id`, each appearing EXACTLY ONCE
    /// (Bug #1929 item 2 -- previously one entry PER CALL SITE, which
    /// inflated "how many callees does this have" for any caller that
    /// counts the result; reported live against a real-world
    /// `Reader.consumeToAny`, where `callers_of` length (5) did not
    /// match the distinct-caller count (3)). Deduplicated in FIRST-SEEN
    /// CSR order (never a
    /// `HashSet`'s unspecified iteration order), mirroring `AdjacencyIndex
    /// ::filtered_edges_of`'s identical dedup contract. M2 fix: O(out-
    /// degree) via the precomputed CSR forward adjacency index
    /// (`callees_index`, built ONCE at graph construction) -- NEVER a
    /// re-scan of every reference in the repository, which made this (and
    /// every caller that queries it once per node: `reachable_from`,
    /// `shortest_path_to_any`, `strongly_connected_components`) O(V*E)
    /// before that fix; the dedup pass here adds only O(out-degree),
    /// never a second scan of anything larger.
    pub fn callees_of(&self, dense_symbol_id: u32) -> Vec<u32> {
        dedup_preserving_first_seen_order(self.callees_index(dense_symbol_id))
    }

    /// Every symbol (dense id) that has at least one reference proposing
    /// `dense_symbol_id` as a candidate target -- the reverse of
    /// `callees_of`, each appearing EXACTLY ONCE for the identical Bug
    /// #1929 item 2 reason `callees_of` documents above. M2 fix: O(in-
    /// degree) via the precomputed CSR reverse adjacency index, same
    /// rationale as `callees_of` above.
    pub fn callers_of(&self, dense_symbol_id: u32) -> Vec<u32> {
        dedup_preserving_first_seen_order(self.callers_index(dense_symbol_id))
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
        strongly_connected_components_over_adjacency(n, &adjacency)
    }
}

/// #1924/#1925 (epic #1906): the Tarjan walk itself, factored out of
/// `strongly_connected_components` so `ops_filtered.rs`'s `strongly_
/// connected_components_filtered` can reuse the IDENTICAL algorithm over a
/// pre-computed FILTERED adjacency list, rather than duplicating ~80 lines
/// of iterative Tarjan logic (Rule 4, anti-duplication) -- the only thing
/// that differs between the unfiltered and filtered SCC primitives is HOW
/// `adjacency` was built (`callees_of` vs `callees_of_filtered`), never the
/// traversal that consumes it.
pub(super) fn strongly_connected_components_over_adjacency(n: usize, adjacency: &[Vec<u32>]) -> Vec<Vec<u32>> {
    let mut ctx = TarjanContext::new(n);
    for start in 0..n as u32 {
        if ctx.index[start as usize].is_none() {
            ctx.run_from(start, adjacency);
        }
    }
    ctx.components
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

    /// Bug #1929 item 2: `callers_of` must return each caller EXACTLY
    /// ONCE, never one entry PER CALL SITE -- reported live against a
    /// real-world `Reader.consumeToAny`, where `callers_of` length (5) did
    /// not match the distinct-caller count (3), inflating "how many
    /// callers does this have" for anyone counting the result. A calls C
    /// from THREE separate call sites (three distinct `Reference`s, same
    /// `from`/`to` pair) -- the pre-fix implementation returns A three
    /// times; the fix must return it once.
    #[test]
    fn callers_of_deduplicates_a_caller_with_multiple_call_sites_to_the_same_target() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
        let a = builder.intern_symbol(make_symbol_id(10, 0));
        let c = builder.intern_symbol(make_symbol_id(10, 1));

        builder.add_reference(a, 10, 1, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
        builder.add_reference(a, 10, 2, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
        builder.add_reference(a, 10, 3, 0, &[Candidate::new(c, reasons::SAME_FILE)]);

        let graph = builder.build();

        assert_eq!(
            graph.callers_of(c),
            vec![a],
            "A calls C from 3 call sites but must appear as a caller exactly once"
        );
    }

    /// Bug #1929 item 2, the `callees_of` mirror: B is called from the
    /// SAME enclosing symbol A across two separate call sites -- A's
    /// callee list must list B once, not twice.
    #[test]
    fn callees_of_deduplicates_a_callee_reached_from_multiple_call_sites() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
        let a = builder.intern_symbol(make_symbol_id(11, 0));
        let b = builder.intern_symbol(make_symbol_id(11, 1));

        builder.add_reference(a, 11, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        builder.add_reference(a, 11, 2, 0, &[Candidate::new(b, reasons::SAME_FILE)]);

        let graph = builder.build();

        assert_eq!(
            graph.callees_of(a),
            vec![b],
            "A calls B from 2 call sites but must appear as a callee exactly once"
        );
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

    /// Bug #1901 AC: `reachable_to(targets, max_depth)` is the CALLERS-
    /// direction mirror of `reachable_from` -- same bounded-BFS semantics
    /// (root-inclusive, monotonic, convergent), just walked backward over
    /// `callers_of` instead of forward over `callees_of`. Reuses the EXACT
    /// SAME diamond-plus-cycle fixture `reachable_from_stops_at_max_depth_
    /// and_never_visits_a_node_twice` builds (A->B, A->C, B->D, C->D, D->A
    /// cycle, D->E), but queries it from the OPPOSITE end: starting at E
    /// and walking callers backward reaches D, then D's callers {B, C},
    /// then their caller A (found once despite two paths), then A's own
    /// caller D again via the cycle (already visited) -- the identical
    /// node set {A,B,C,D,E} `reachable_from(&[a], ..)` finds, proving the
    /// two traversals are true mirror images. This is what would catch a
    /// wrong implementation that reused `callees_of` by mistake (forward
    /// direction, from D backward would find no callers via a forward
    /// scan) or omitted the visited-set (infinite loop on the D<->A cycle)
    /// or the depth check (A would leak into a max_depth=2 query, which is
    /// beyond its true 3-hop distance from E).
    #[test]
    fn reachable_to_mirrors_reachable_from_over_the_reverse_adjacency() {
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

        let reached = graph.reachable_to(&[e], 100);
        let mut sorted = reached.clone();
        sorted.sort_unstable();
        assert_eq!(sorted, vec![a, b, c, d, e], "every caller transitively reachable, each exactly once");
        for node in [a, b, c, d, e] {
            assert_eq!(
                reached.iter().filter(|&&n| n == node).count(),
                1,
                "node {node} must appear exactly once despite being reachable via two paths / a cycle"
            );
        }

        // A is 3 hops from E along the shortest caller-chain (E <- D <- {B
        // or C} <- A); max_depth=2 must exclude it while still including
        // the diamond of callers.
        let mut bounded = graph.reachable_to(&[e], 2);
        bounded.sort_unstable();
        assert_eq!(bounded, vec![b, c, d, e], "A is 3 hops away, beyond max_depth=2");
    }

    /// Bug #1901 Part A/B: the documented blast-radius symptom in one test.
    /// A Type-node root (a class with zero outbound references, the shape
    /// every class-level symbol carries) returns ONLY itself from
    /// `reachable_from` -- correct, documented behaviour, not a bug -- while
    /// pointing `reachable_to` at a REAL method root finds its true
    /// transitive callers. `controller` is a `DeclarationKind::Type` with no
    /// callees at all; `handler` is a method called by `router`, which is
    /// itself called by `entrypoint`.
    #[test]
    fn reachable_from_on_a_type_root_returns_only_itself_while_reachable_to_on_a_method_root_finds_real_callers() {
        use crate::graph::extract::local_index::DeclarationKind;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
        let controller = builder.intern_symbol(make_symbol_id(1, 0));
        builder.add_kind(controller, DeclarationKind::Type);
        // A Type declaration carries no outbound reference of its own --
        // exactly the documented shape ("Type ... nodes carry no outbound
        // edges") that makes reachable_from(roots=[a class], ..) silently
        // return just the root, with nothing in the response flagging the
        // root kind as unsuitable.

        let handler = builder.intern_symbol(make_symbol_id(2, 0));
        let router = builder.intern_symbol(make_symbol_id(2, 1));
        let entrypoint = builder.intern_symbol(make_symbol_id(2, 2));
        builder.add_kind(handler, DeclarationKind::Method);
        builder.add_reference(router, 2, 1, 0, &[Candidate::new(handler, reasons::SAME_FILE)]);
        builder.add_reference(entrypoint, 2, 2, 0, &[Candidate::new(router, reasons::SAME_FILE)]);

        let graph = builder.build();

        assert_eq!(
            graph.reachable_from(&[controller], 5),
            vec![controller],
            "a Type root with zero outbound edges silently returns only itself from reachable_from -- \
             documented, correct behaviour, never a bug in reachable_from itself"
        );

        let mut blast_radius = graph.reachable_to(&[handler], 5);
        blast_radius.sort_unstable();
        assert_eq!(
            blast_radius,
            vec![handler, router, entrypoint],
            "reachable_to on a real method root finds its true transitive callers -- \
             this is the primitive the documented blast-radius use case actually needs"
        );
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
