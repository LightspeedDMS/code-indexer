//! CSR (compressed sparse row) memory layout for the whole-repository code
//! graph (Story #1787, S2, AC5).
//!
//! `graph.candidates: Vec<Candidate>` is ONE flat allocation for the entire
//! repository -- never one `Vec<Candidate>` per reference. Each `Reference`
//! records a `(cand_start, cand_len)` window into that single arena instead
//! of owning its own candidate collection. See `builder::CodeGraphBuilder`
//! for how that single allocation is reserved up front and never
//! reallocated while appending, and `code_graph::CodeGraph` for the
//! resulting immutable, query-only graph.

pub mod reference;
pub mod candidate;
pub mod symbol_table;
pub mod builder;
pub mod code_graph;
pub mod ops;
pub mod wire;
mod wire_cursor;

pub use candidate::Candidate;
pub use code_graph::CodeGraph;
pub use reference::Reference;
pub use builder::CodeGraphBuilder;
pub use handle::GraphHandle;

/// ADR-002 / Story #1787 AC8: the opaque `GraphHandle` accessor ABI.
/// Evaluator dylibs receive this handle instead of `&CodeGraph` directly --
/// it carries an opaque context pointer plus accessor FUNCTION POINTERS
/// bound to the AC7 bounded ops already proven in `super::ops`, never a
/// mirrored `CodeGraph` layout. See `docs/adr/ADR-002-xray-graph-handle-ffi.md`.
///
/// Kept INLINE in this file (rather than a separate `handle.rs`) purely so
/// its definition and its wiring into the crate's module tree land in the
/// same, always-compilable change; there is no other reason to prefer this
/// over a sibling file, and it may be split out later once the whole
/// accessor surface is in place.
///
/// Plain Rust `fn` pointers (not `extern "C"`/`#[repr(C)]`) deliberately
/// continue the SAME calling convention `dynlib.rs`'s
/// `EvaluateNodeFn = fn(&OwnedNode) -> Vec<EvalFinding>` already uses at
/// this crate's one existing dylib boundary: both sides are compiled by the
/// identical rustc invocation (verified via `XRAY_ABI_VERSION` and the
/// recorded `rustc_version`, Bug #1784), so the plain Rust ABI is already a
/// proven-safe convention here. Introducing a second, C-shaped convention
/// alongside it would duplicate that safety reasoning for no new guarantee
/// (Rule 4, anti-duplication) and would be a larger, unrequested redesign
/// than this ABI slice calls for (Rule 9, anti-divergent-creativity).
pub mod handle {
    use super::code_graph::CodeGraph;
    use std::marker::PhantomData;

    type CtxPtr = *const ();
    /// Raw `(ptr, len)` parts of a `&str` borrowed from the graph's shared
    /// string table -- see `GraphHandle::resolve_string` for why a bare
    /// `fn` pointer type cannot return `&str` directly here.
    type RawStr = (*const u8, usize);

    /// The opaque handle. `'graph` is enforced by the borrow checker via
    /// the `PhantomData` marker below: a `GraphHandle<'graph>` cannot
    /// outlive the `CodeGraph` it was built from. The marker's `()` target
    /// deliberately does not name `CodeGraph`, so a PREAMBLE mirror can
    /// declare a field-for-field identical struct without needing to know
    /// that type. `Copy`/`Clone` because every field is a plain pointer or
    /// function pointer -- no owned/heap data, so passing it by value never
    /// has drop/double-free concerns the way a mirrored `CodeGraph` would.
    /// The 7 accessor fields are spelled out with RAW `fn(...)->...` types
    /// here (not via the `type X = ...` aliases above), deliberately --
    /// `preamble_ac18_parity.rs`'s structural comparison diffs each
    /// field's `syn::Type` AST as written, so two sides both writing a
    /// bare alias IDENT (e.g. `callees_of_fn: CalleesOfFn`) would compare
    /// EQUAL even if the two aliases' own expansions silently diverged --
    /// exactly the blind spot AC18 exists to close. `ctx`'s alias
    /// (`CtxPtr = *const ()`) is trivially unambiguous and stays aliased.
    // clippy::type_complexity: the raw (non-aliased) fn-pointer field types
    // are DELIBERATE (see the doc comment above) so the AC18 structural
    // parity check has no alias-name blind spot -- allowed here rather
    // than "fixed" by reintroducing the aliases clippy wants, which would
    // reopen exactly that blind spot.
    #[allow(clippy::type_complexity)]
    #[derive(Clone, Copy)]
    pub struct GraphHandle<'graph> {
        ctx: CtxPtr,
        callees_of_fn: fn(*const (), u32) -> Vec<u32>,
        callers_of_fn: fn(*const (), u32) -> Vec<u32>,
        reachable_from_fn: fn(*const (), &[u32], usize) -> Vec<u32>,
        shortest_path_to_any_fn: fn(*const (), u32, &[u32], usize) -> Option<Vec<u32>>,
        strongly_connected_components_fn: fn(*const ()) -> Vec<Vec<u32>>,
        resolve_symbol_fn: fn(*const (), u32) -> u64,
        resolve_string_raw_fn: fn(*const (), u32) -> (*const u8, usize),
        _graph: PhantomData<&'graph ()>,
    }

    /// Reinterprets the opaque context pointer back as `&CodeGraph`.
    /// SAFETY: only ever reachable through a `GraphHandle` built by
    /// `GraphHandle::from_graph`, which sets `ctx` from a real `&'graph
    /// CodeGraph` -- the `'graph` borrow-checker contract on `GraphHandle`
    /// itself is what guarantees that reference is still valid at call time.
    fn graph_from_ctx<'a>(ctx: CtxPtr) -> &'a CodeGraph {
        unsafe { &*(ctx as *const CodeGraph) }
    }

    fn thunk_callees_of(ctx: CtxPtr, symbol: u32) -> Vec<u32> {
        graph_from_ctx(ctx).callees_of(symbol)
    }

    fn thunk_callers_of(ctx: CtxPtr, symbol: u32) -> Vec<u32> {
        graph_from_ctx(ctx).callers_of(symbol)
    }

    fn thunk_reachable_from(ctx: CtxPtr, roots: &[u32], max_depth: usize) -> Vec<u32> {
        graph_from_ctx(ctx).reachable_from(roots, max_depth)
    }

    fn thunk_shortest_path_to_any(ctx: CtxPtr, from: u32, targets: &[u32], max_depth: usize) -> Option<Vec<u32>> {
        graph_from_ctx(ctx).shortest_path_to_any(from, targets, max_depth)
    }

    fn thunk_strongly_connected_components(ctx: CtxPtr) -> Vec<Vec<u32>> {
        graph_from_ctx(ctx).strongly_connected_components()
    }

    fn thunk_resolve_symbol(ctx: CtxPtr, dense_id: u32) -> u64 {
        graph_from_ctx(ctx).resolve_symbol(dense_id)
    }

    fn thunk_resolve_string_raw(ctx: CtxPtr, string_id: u32) -> RawStr {
        let s = graph_from_ctx(ctx).resolve_string(string_id);
        (s.as_ptr(), s.len())
    }

    impl<'graph> GraphHandle<'graph> {
        /// Builds a handle bound to `graph`. The `'graph` lifetime
        /// parameter is what makes the SAFETY contract above a
        /// BORROW-CHECKED guarantee: the returned handle cannot outlive
        /// `graph`.
        pub fn from_graph(graph: &'graph CodeGraph) -> GraphHandle<'graph> {
            GraphHandle {
                ctx: graph as *const CodeGraph as CtxPtr,
                callees_of_fn: thunk_callees_of,
                callers_of_fn: thunk_callers_of,
                reachable_from_fn: thunk_reachable_from,
                shortest_path_to_any_fn: thunk_shortest_path_to_any,
                strongly_connected_components_fn: thunk_strongly_connected_components,
                resolve_symbol_fn: thunk_resolve_symbol,
                resolve_string_raw_fn: thunk_resolve_string_raw,
                _graph: PhantomData,
            }
        }

        pub fn callees_of(&self, symbol: u32) -> Vec<u32> {
            (self.callees_of_fn)(self.ctx, symbol)
        }

        pub fn callers_of(&self, symbol: u32) -> Vec<u32> {
            (self.callers_of_fn)(self.ctx, symbol)
        }

        pub fn reachable_from(&self, roots: &[u32], max_depth: usize) -> Vec<u32> {
            (self.reachable_from_fn)(self.ctx, roots, max_depth)
        }

        pub fn shortest_path_to_any(&self, from: u32, targets: &[u32], max_depth: usize) -> Option<Vec<u32>> {
            (self.shortest_path_to_any_fn)(self.ctx, from, targets, max_depth)
        }

        pub fn strongly_connected_components(&self) -> Vec<Vec<u32>> {
            (self.strongly_connected_components_fn)(self.ctx)
        }

        pub fn resolve_symbol(&self, dense_id: u32) -> u64 {
            (self.resolve_symbol_fn)(self.ctx, dense_id)
        }

        /// Returns a `&str` borrowed from the graph's shared string table,
        /// with a lifetime tied to `&self` -- never an owned `String`
        /// (AC5/ADR-002).
        pub fn resolve_string(&self, string_id: u32) -> &str {
            let (ptr, len) = (self.resolve_string_raw_fn)(self.ctx, string_id);
            // SAFETY: `ptr`/`len` come from `CodeGraph::resolve_string`'s
            // own `&str` (via `thunk_resolve_string_raw`), guaranteed valid
            // UTF-8 and alive for at least `'graph` -- which, per this
            // struct's borrow-checked lifetime contract, outlives `&self`.
            unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use crate::graph::csr::builder::CodeGraphBuilder;
        use crate::graph::csr::candidate::Candidate;
        use crate::graph::identity::make_symbol_id;
        use crate::graph::reasons;

        #[test]
        fn callees_of_and_callers_of_delegate_to_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
            let a = builder.intern_symbol(make_symbol_id(1, 0));
            let b = builder.intern_symbol(make_symbol_id(1, 1));
            let c = builder.intern_symbol(make_symbol_id(1, 2));
            builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
            builder.add_reference(a, 1, 2, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
            let graph = builder.build();

            let handle = GraphHandle::from_graph(&graph);

            let mut via_handle = handle.callees_of(a);
            via_handle.sort_unstable();
            let mut via_graph = graph.callees_of(a);
            via_graph.sort_unstable();
            assert_eq!(via_handle, via_graph);
            assert_eq!(via_handle, vec![b, c]);

            assert_eq!(handle.callers_of(c), graph.callers_of(c));
            assert_eq!(handle.callers_of(c), vec![a]);
        }

        /// Diamond A->B->D and A->C->D, plus a cycle D->A, matching
        /// `csr::ops`'s own fixture -- proves `reachable_from`,
        /// `shortest_path_to_any`, and `strongly_connected_components` all
        /// delegate to the real bounded ops rather than reimplementing
        /// (and potentially diverging from) their traversal logic.
        #[test]
        fn reachable_from_and_shortest_path_and_scc_delegate_to_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(5);
            let a = builder.intern_symbol(make_symbol_id(1, 0));
            let b = builder.intern_symbol(make_symbol_id(1, 1));
            let c = builder.intern_symbol(make_symbol_id(1, 2));
            let d = builder.intern_symbol(make_symbol_id(1, 3));
            builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
            builder.add_reference(a, 1, 2, 0, &[Candidate::new(c, reasons::SAME_FILE)]);
            builder.add_reference(b, 1, 3, 0, &[Candidate::new(d, reasons::SAME_FILE)]);
            builder.add_reference(c, 1, 4, 0, &[Candidate::new(d, reasons::SAME_FILE)]);
            builder.add_reference(d, 1, 5, 0, &[Candidate::new(a, reasons::SAME_FILE)]);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            let mut via_handle = handle.reachable_from(&[a], 100);
            via_handle.sort_unstable();
            let mut via_graph = graph.reachable_from(&[a], 100);
            via_graph.sort_unstable();
            assert_eq!(via_handle, via_graph);
            assert_eq!(via_handle, vec![a, b, c, d]);
            assert_eq!(handle.reachable_from(&[a], 0), vec![a]);

            assert_eq!(
                handle.shortest_path_to_any(a, &[d], 100),
                graph.shortest_path_to_any(a, &[d], 100)
            );
            assert_eq!(handle.shortest_path_to_any(a, &[d], 1), None);

            let mut via_handle_scc = handle.strongly_connected_components();
            let mut via_graph_scc = graph.strongly_connected_components();
            for comp in via_handle_scc.iter_mut().chain(via_graph_scc.iter_mut()) {
                comp.sort_unstable();
            }
            via_handle_scc.sort();
            via_graph_scc.sort();
            assert_eq!(via_handle_scc, via_graph_scc);
            assert_eq!(via_handle_scc.len(), 1, "a-b-d, a-c-d, d-a form one strongly connected component");
        }

        #[test]
        fn resolve_symbol_and_resolve_string_borrow_from_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
            let a = builder.intern_symbol(make_symbol_id(1, 0));
            let foo = builder.intern_string("Foo");
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(handle.resolve_symbol(a), graph.resolve_symbol(a));
            assert_eq!(handle.resolve_string(foo), "Foo");
            assert_eq!(handle.resolve_string(foo), graph.resolve_string(foo));
        }
    }
}
