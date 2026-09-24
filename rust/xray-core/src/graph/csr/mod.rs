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
mod adjacency;
pub mod builder;
pub mod code_graph;
pub mod ops;
pub mod ops_filtered;
pub mod wire;
mod wire_cursor;

pub use candidate::Candidate;
pub use code_graph::{CodeGraph, EdgeReason};
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
/// this crate's one existing dylib boundary. This is NOT because "both
/// sides are compiled by the identical rustc invocation, verified via
/// `XRAY_ABI_VERSION` and the recorded `rustc_version`" -- that reasoning
/// is circular (Bug #1855 review): the probe that is supposed to ESTABLISH
/// compatibility cannot itself presume compatibility, and re-checking
/// `XRAY_ABI_VERSION`/`rustc_version` cannot rescue it either, since
/// `compute_cache_identity`'s `rustc_version` is pinned to the toolchain
/// channel and therefore reads the identical value on both sides of a
/// genuine host/evaluator mismatch -- it structurally cannot detect the
/// failure mode this doc comment used to claim it ruled out.
///
/// The real reason the plain Rust ABI is safe here: `GraphHandle`/
/// `FactsHandle` accessors, like every data-carrying dylib callback, are
/// only ever reached AFTER `dynlib.rs`'s loader has ALREADY verified
/// compatibility through a probe (`xray_abi_version`, then
/// `xray_rustc_version_ptr`/`_len`) that is itself `extern "C"` -- a fixed,
/// documented calling convention chosen (Bug #1855, H1) specifically so
/// that probe does not need to presume the very compatibility it exists to
/// prove. Once that probe passes, the plain Rust ABI is a proven-safe
/// convention for THIS SPECIFIC loaded `.so`, not an assumed one.
/// Introducing a second, C-shaped convention for these accessors
/// themselves would duplicate that safety reasoning for no new guarantee
/// (Rule 4, anti-duplication) and would be a larger, unrequested redesign
/// than this ABI slice calls for (Rule 9, anti-divergent-creativity).
pub mod handle {
    use super::code_graph::{CodeGraph, EdgeReason};
    use crate::graph::extract::local_index::{DeclarationKind, Visibility};
    use crate::graph::identity::SymbolId;
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
    /// The accessor fields are spelled out with RAW `fn(...)->...` types
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
        /// Bug #1901: the CALLERS-direction counterpart of
        /// `reachable_from_fn` -- see `GraphHandle::reachable_to` for the
        /// full rationale.
        reachable_to_fn: fn(*const (), &[u32], usize) -> Vec<u32>,
        shortest_path_to_any_fn: fn(*const (), u32, &[u32], usize) -> Option<Vec<u32>>,
        strongly_connected_components_fn: fn(*const ()) -> Vec<Vec<u32>>,
        resolve_symbol_fn: fn(*const (), u32) -> Option<u64>,
        resolve_string_raw_fn: fn(*const (), u32) -> Option<(*const u8, usize)>,
        is_symbol_referenced_fn: fn(*const (), u32) -> bool,
        is_definitely_dead_code_fn: fn(*const (), u32) -> Option<bool>,
        /// Story #1792 (S3, AC4): cross-file captioning without re-parsing --
        /// see `GraphHandle::signature_for`'s doc comment.
        signature_for_raw_fn: fn(*const (), u32) -> Option<(*const u8, usize)>,
        symbol_count_fn: fn(*const ()) -> usize,
        dense_id_for_fn: fn(*const (), u64) -> Option<u32>,
        /// Bug #1900 (epic #1906 P5): raw `(ptr, len, line)` parts of a
        /// symbol's declaration location -- see `GraphHandle::location_for`
        /// for why a bare `fn` pointer cannot return a borrowed `&str`
        /// directly here, mirroring `signature_for_raw_fn` exactly.
        location_for_raw_fn: fn(*const (), u32) -> Option<(*const u8, usize, usize)>,
        /// Bug #1900: exposes `CodeGraph::kind_for` -- see
        /// `GraphHandle::declaration_kind`.
        declaration_kind_fn: fn(*const (), u32) -> Option<DeclarationKind>,
        /// Bug #1900: exposes `CodeGraph::visibility_for` -- see
        /// `GraphHandle::visibility_of`.
        visibility_of_fn: fn(*const (), u32) -> Visibility,
        /// Bug #1900: exposes `CodeGraph::edge_reason` -- see
        /// `GraphHandle::edge_reason`.
        edge_reason_fn: fn(*const (), u32, u32) -> Option<EdgeReason>,
        /// Bug #1900 (review round 2): exposes `CodeGraph::edge_evidence` --
        /// see `GraphHandle::edge_evidence`.
        edge_evidence_fn: fn(*const (), u32, u32) -> Option<u16>,
        /// #1924/#1925 (epic #1906): exposes `CodeGraph::callees_of_filtered`
        /// -- see `GraphHandle::callees_of_filtered`.
        callees_of_filtered_fn: fn(*const (), u32, u16, u16) -> Vec<u32>,
        /// #1924/#1925: exposes `CodeGraph::callers_of_filtered` -- see
        /// `GraphHandle::callers_of_filtered`.
        callers_of_filtered_fn: fn(*const (), u32, u16, u16) -> Vec<u32>,
        /// #1924/#1925: exposes `CodeGraph::reachable_from_filtered` -- see
        /// `GraphHandle::reachable_from_filtered`.
        reachable_from_filtered_fn: fn(*const (), &[u32], usize, u16, u16) -> Vec<u32>,
        /// #1924/#1925: exposes `CodeGraph::reachable_to_filtered` -- see
        /// `GraphHandle::reachable_to_filtered`.
        reachable_to_filtered_fn: fn(*const (), &[u32], usize, u16, u16) -> Vec<u32>,
        /// #1924/#1925: exposes `CodeGraph::strongly_connected_components_
        /// filtered` -- see `GraphHandle::strongly_connected_components_
        /// filtered`.
        strongly_connected_components_filtered_fn: fn(*const (), u16, u16) -> Vec<Vec<u32>>,
        /// #1953: exposes `CodeGraph::shortest_path_to_any_filtered` -- see
        /// `GraphHandle::shortest_path_to_any_filtered`.
        shortest_path_to_any_filtered_fn: fn(*const (), u32, &[u32], usize, u16, u16) -> Option<Vec<u32>>,
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

    /// Bug #1901: thunk for `reachable_to` -- mirrors `thunk_reachable_from`
    /// exactly, delegating to `CodeGraph::reachable_to`.
    fn thunk_reachable_to(ctx: CtxPtr, targets: &[u32], max_depth: usize) -> Vec<u32> {
        graph_from_ctx(ctx).reachable_to(targets, max_depth)
    }

    fn thunk_shortest_path_to_any(ctx: CtxPtr, from: u32, targets: &[u32], max_depth: usize) -> Option<Vec<u32>> {
        graph_from_ctx(ctx).shortest_path_to_any(from, targets, max_depth)
    }

    fn thunk_strongly_connected_components(ctx: CtxPtr) -> Vec<Vec<u32>> {
        graph_from_ctx(ctx).strongly_connected_components()
    }

    fn thunk_resolve_symbol(ctx: CtxPtr, dense_id: u32) -> Option<u64> {
        graph_from_ctx(ctx).try_resolve_symbol(dense_id)
    }

    fn thunk_resolve_string_raw(ctx: CtxPtr, string_id: u32) -> Option<RawStr> {
        graph_from_ctx(ctx).try_resolve_string(string_id).map(|s| (s.as_ptr(), s.len()))
    }

    /// D2 fix (dual-review Critical): exposes the AC6 decoupled
    /// referenced-bit through the ONLY surface `analyze_graph` ever
    /// receives. See `CodeGraph::is_symbol_referenced` -- the bit was
    /// marked from the RAW candidate list before any ladder capping, so it
    /// survives candidate-arena truncation even when `callers_of` (which
    /// reads the POST-CAP arena) reports zero callers for the same symbol.
    fn thunk_is_symbol_referenced(ctx: CtxPtr, dense_id: u32) -> bool {
        graph_from_ctx(ctx).is_symbol_referenced(dense_id)
    }

    /// D2 fix: exposes the dead-code verdict. See
    /// `CodeGraph::is_definitely_dead_code` -- `Some(false)` for any
    /// referenced symbol regardless of completeness; `Some(true)` (Story
    /// #1835) for an unreferenced symbol PROVABLY not externally visible
    /// (Java `private`); `None` for every other unreferenced symbol (Bug
    /// #1833: `Public`/`Protected`/`Unknown` visibility carries no in-repo
    /// evidence that can prove unreachability from outside the repo).
    fn thunk_is_definitely_dead_code(ctx: CtxPtr, dense_id: u32) -> Option<bool> {
        graph_from_ctx(ctx).is_definitely_dead_code(dense_id)
    }

    /// Story #1792 (S3, AC4): exposes `CodeGraph::signature_for` -- the
    /// per-symbol cached signature line captured at extraction -- through
    /// the ONLY surface a graph-mode evaluator ever receives. Mirrors
    /// `thunk_resolve_string_raw`'s exact raw-parts shape for the same
    /// reason: a bare `fn` pointer cannot return a borrowed `&str` directly.
    fn thunk_signature_for_raw(ctx: CtxPtr, dense_id: u32) -> Option<RawStr> {
        graph_from_ctx(ctx).signature_for(dense_id).map(|s| (s.as_ptr(), s.len()))
    }

    fn thunk_symbol_count(ctx: CtxPtr) -> usize {
        graph_from_ctx(ctx).symbol_count()
    }

    fn thunk_dense_id_for(ctx: CtxPtr, symbol: u64) -> Option<u32> {
        graph_from_ctx(ctx).dense_id_for(symbol)
    }

    /// Bug #1900: raw-parts thunk for `location_for` -- mirrors
    /// `thunk_signature_for_raw` exactly.
    fn thunk_location_for_raw(ctx: CtxPtr, dense_id: u32) -> Option<(*const u8, usize, usize)> {
        let (path, line) = graph_from_ctx(ctx).location_for(dense_id)?;
        Some((path.as_ptr(), path.len(), line))
    }

    fn thunk_declaration_kind(ctx: CtxPtr, dense_id: u32) -> Option<DeclarationKind> {
        graph_from_ctx(ctx).kind_for(dense_id)
    }

    fn thunk_visibility_of(ctx: CtxPtr, dense_id: u32) -> Visibility {
        graph_from_ctx(ctx).visibility_for(dense_id)
    }

    fn thunk_edge_reason(ctx: CtxPtr, from: u32, to: u32) -> Option<EdgeReason> {
        graph_from_ctx(ctx).edge_reason(from, to)
    }

    /// Bug #1900 (review round 2): thunk for `edge_evidence` -- mirrors
    /// `thunk_edge_reason` exactly.
    fn thunk_edge_evidence(ctx: CtxPtr, from: u32, to: u32) -> Option<u16> {
        graph_from_ctx(ctx).edge_evidence(from, to)
    }

    /// #1924/#1925: thunk for `callees_of_filtered`.
    fn thunk_callees_of_filtered(ctx: CtxPtr, symbol: u32, required: u16, forbidden: u16) -> Vec<u32> {
        graph_from_ctx(ctx).callees_of_filtered(symbol, required, forbidden)
    }

    /// #1924/#1925: thunk for `callers_of_filtered`.
    fn thunk_callers_of_filtered(ctx: CtxPtr, symbol: u32, required: u16, forbidden: u16) -> Vec<u32> {
        graph_from_ctx(ctx).callers_of_filtered(symbol, required, forbidden)
    }

    /// #1924/#1925: thunk for `reachable_from_filtered`.
    fn thunk_reachable_from_filtered(ctx: CtxPtr, roots: &[u32], max_depth: usize, required: u16, forbidden: u16) -> Vec<u32> {
        graph_from_ctx(ctx).reachable_from_filtered(roots, max_depth, required, forbidden)
    }

    /// #1924/#1925: thunk for `reachable_to_filtered`.
    fn thunk_reachable_to_filtered(ctx: CtxPtr, targets: &[u32], max_depth: usize, required: u16, forbidden: u16) -> Vec<u32> {
        graph_from_ctx(ctx).reachable_to_filtered(targets, max_depth, required, forbidden)
    }

    /// #1924/#1925: thunk for `strongly_connected_components_filtered`.
    fn thunk_strongly_connected_components_filtered(ctx: CtxPtr, required: u16, forbidden: u16) -> Vec<Vec<u32>> {
        graph_from_ctx(ctx).strongly_connected_components_filtered(required, forbidden)
    }

    /// #1953: thunk for `shortest_path_to_any_filtered`.
    fn thunk_shortest_path_to_any_filtered(
        ctx: CtxPtr,
        from: u32,
        targets: &[u32],
        max_depth: usize,
        required: u16,
        forbidden: u16,
    ) -> Option<Vec<u32>> {
        graph_from_ctx(ctx).shortest_path_to_any_filtered(from, targets, max_depth, required, forbidden)
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
                reachable_to_fn: thunk_reachable_to,
                shortest_path_to_any_fn: thunk_shortest_path_to_any,
                strongly_connected_components_fn: thunk_strongly_connected_components,
                resolve_symbol_fn: thunk_resolve_symbol,
                resolve_string_raw_fn: thunk_resolve_string_raw,
                is_symbol_referenced_fn: thunk_is_symbol_referenced,
                is_definitely_dead_code_fn: thunk_is_definitely_dead_code,
                signature_for_raw_fn: thunk_signature_for_raw,
                symbol_count_fn: thunk_symbol_count,
                dense_id_for_fn: thunk_dense_id_for,
                location_for_raw_fn: thunk_location_for_raw,
                declaration_kind_fn: thunk_declaration_kind,
                visibility_of_fn: thunk_visibility_of,
                edge_reason_fn: thunk_edge_reason,
                edge_evidence_fn: thunk_edge_evidence,
                callees_of_filtered_fn: thunk_callees_of_filtered,
                callers_of_filtered_fn: thunk_callers_of_filtered,
                reachable_from_filtered_fn: thunk_reachable_from_filtered,
                reachable_to_filtered_fn: thunk_reachable_to_filtered,
                strongly_connected_components_filtered_fn: thunk_strongly_connected_components_filtered,
                shortest_path_to_any_filtered_fn: thunk_shortest_path_to_any_filtered,
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

        /// Bug #1901: the CALLERS-direction counterpart of `reachable_from`
        /// -- "how much of the codebase can a change to `targets` affect" is
        /// the transitive CALLERS closure, the direction `reachable_from`
        /// cannot express (it follows `callees_of`, "what the roots depend
        /// on"). Same bounded-BFS semantics as `reachable_from`
        /// (root-inclusive, monotonic, convergent) -- see
        /// `CodeGraph::reachable_to`'s doc comment for the full rationale.
        pub fn reachable_to(&self, targets: &[u32], max_depth: usize) -> Vec<u32> {
            (self.reachable_to_fn)(self.ctx, targets, max_depth)
        }

        pub fn shortest_path_to_any(&self, from: u32, targets: &[u32], max_depth: usize) -> Option<Vec<u32>> {
            (self.shortest_path_to_any_fn)(self.ctx, from, targets, max_depth)
        }

        pub fn strongly_connected_components(&self) -> Vec<Vec<u32>> {
            (self.strongly_connected_components_fn)(self.ctx)
        }

        pub fn resolve_symbol(&self, dense_id: u32) -> Option<u64> {
            (self.resolve_symbol_fn)(self.ctx, dense_id)
        }

        pub fn symbol_count(&self) -> usize {
            (self.symbol_count_fn)(self.ctx)
        }

        pub fn dense_id_for(&self, symbol: SymbolId) -> Option<u32> {
            (self.dense_id_for_fn)(self.ctx, symbol)
        }

        /// Returns a `&str` borrowed from the graph's shared string table,
        /// with a lifetime tied to `&self` -- never an owned `String`
        /// (AC5/ADR-002). `None` on an out-of-range `string_id` (ADR-002
        /// Defect 2 fix) rather than panicking.
        pub fn resolve_string(&self, string_id: u32) -> Option<&str> {
            let (ptr, len) = (self.resolve_string_raw_fn)(self.ctx, string_id)?;
            // SAFETY: `ptr`/`len` come from `CodeGraph::try_resolve_string`'s
            // own `&str` (via `thunk_resolve_string_raw`), guaranteed valid
            // UTF-8 and alive for at least `'graph` -- which, per this
            // struct's borrow-checked lifetime contract, outlives `&self`.
            Some(unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) })
        }

        /// D2 fix (dual-review Critical): "is this symbol referenced by
        /// ANY raw candidate, even one that was later capped away by the
        /// AC6 budget ladder?" -- see `CodeGraph::is_symbol_referenced`.
        /// Unlike `callers_of(symbol).is_empty()`, this reads the
        /// decoupled, pre-cap `ReferencedBits` state, so it never goes
        /// blind under budget pressure.
        pub fn is_symbol_referenced(&self, dense_id: u32) -> bool {
            (self.is_symbol_referenced_fn)(self.ctx, dense_id)
        }

        /// D2 fix: the dead-code verdict -- see
        /// `CodeGraph::is_definitely_dead_code`. `Some(false)` means
        /// referenced (never dead, regardless of completeness);
        /// `Some(true)` (Story #1835) means unreferenced AND PROVABLY not
        /// externally visible (Java `private`) -- a real dead-code
        /// verdict; `None` means undecidable -- unreferenced with no
        /// in-repo evidence this crate can use to prove the symbol
        /// unreachable from outside the repo (`Public`/`Protected`/
        /// `Unknown` visibility, Bug #1833). This is the ONE surface an
        /// evaluator needs to avoid claiming a confident "dead" verdict
        /// the graph cannot back.
        pub fn is_definitely_dead_code(&self, dense_id: u32) -> Option<bool> {
            (self.is_definitely_dead_code_fn)(self.ctx, dense_id)
        }

        /// Story #1792 (S3, AC4): the symbol's cached AC2 signature line
        /// (`CodeGraph::signature_for`), or `None` when extraction never
        /// captured one -- never fabricated. This is what lets
        /// `analyze_graph`/`refine` caption a cross-file symbol (e.g. the
        /// definition a call site resolves to) WITHOUT adding that file to
        /// the RefineSet: the signature was already cached at extraction
        /// time and travels with the mmap'd graph, so no second parse of
        /// the defining file is ever needed. Returns a `&str` borrowed from
        /// the graph's shared signature storage, with a lifetime tied to
        /// `&self` -- never an owned `String` (AC5/ADR-002), mirroring
        /// `resolve_string`'s exact contract.
        pub fn signature_for(&self, dense_id: u32) -> Option<&str> {
            let (ptr, len) = (self.signature_for_raw_fn)(self.ctx, dense_id)?;
            // SAFETY: identical to `resolve_string` above -- `ptr`/`len`
            // come from `CodeGraph::signature_for`'s own `&str` (via
            // `thunk_signature_for_raw`), guaranteed valid UTF-8 and alive
            // for at least `'graph`, which outlives `&self`.
            Some(unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) })
        }

        /// Bug #1900 (epic #1906 P5): this symbol's DECLARATION file path
        /// and 1-based line (`CodeGraph::location_for`), or `None` if
        /// extraction never recorded one. Returns a `&str` borrowed from
        /// the graph's shared string table, with a lifetime tied to
        /// `&self`, never an owned `String` -- mirrors `resolve_string`/
        /// `signature_for`'s exact contract.
        pub fn location_for(&self, dense_id: u32) -> Option<(&str, usize)> {
            let (ptr, len, line) = (self.location_for_raw_fn)(self.ctx, dense_id)?;
            // SAFETY: identical to `resolve_string`/`signature_for` above --
            // `ptr`/`len` come from `CodeGraph::location_for`'s own `&str`
            // (via `thunk_location_for_raw`), guaranteed valid UTF-8 and
            // alive for at least `'graph`, which outlives `&self`.
            Some((unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) }, line))
        }

        /// Bug #1900: this symbol's extracted `DeclarationKind`
        /// (`CodeGraph::kind_for`), or `None` if the extractor never
        /// recorded one -- see that method's doc comment for why `None`
        /// must be read as "unproven", never as license to report a symbol
        /// dead. Lets an evaluator implementing the "unwired components"
        /// use case filter findings by declaration kind, which was
        /// previously unreachable from `GraphHandle` even though the
        /// dead-code predicate already consults it internally.
        pub fn declaration_kind(&self, dense_id: u32) -> Option<DeclarationKind> {
            (self.declaration_kind_fn)(self.ctx, dense_id)
        }

        /// Bug #1900: this symbol's declared `Visibility`
        /// (`CodeGraph::visibility_for`), defaulting to `Visibility::Unknown`
        /// when the extractor recorded no modifier evidence -- see that
        /// method's doc comment for why `Unknown` is always the safe
        /// default, never a restricted one.
        pub fn visibility_of(&self, dense_id: u32) -> Visibility {
            (self.visibility_of_fn)(self.ctx, dense_id)
        }

        /// Bug #1900 (epic #1906 P2): whether the `(from, to)` edge is
        /// backed by at least one call site where `to` was the reference's
        /// ONLY candidate (`Some(EdgeReason::SoleCandidate)`), every
        /// contributing call site offered several candidates
        /// (`Some(EdgeReason::MultipleCandidates)`), or `from` never
        /// targets `to` at all (`None`) -- a COUNT-based tier only, never a
        /// truth/provenance claim (see `edge_evidence` for that). See
        /// `CodeGraph::edge_reason`'s doc comment for the full rationale
        /// and complexity guarantee: O(out-degree of `from`), cheap for a
        /// path or an SCC member scan, NOT for annotating every edge in
        /// the graph.
        pub fn edge_reason(&self, from: u32, to: u32) -> Option<EdgeReason> {
            (self.edge_reason_fn)(self.ctx, from, to)
        }

        /// Bug #1900 (epic #1906 P2, review round 2): the REAL evidence
        /// accessor -- the bitwise-OR of `graph::reasons::*` bits across
        /// every candidate that contributed the `(from, to)` edge, or
        /// `None` if `from` never targets `to` at all. See
        /// `CodeGraph::edge_evidence`'s doc comment for the full rationale
        /// (this is what lets an evaluator require e.g.
        /// `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO` before trusting a
        /// hop, rather than trusting `edge_reason`'s candidate count
        /// alone) and the same complexity caveat as `edge_reason` above.
        pub fn edge_evidence(&self, from: u32, to: u32) -> Option<u16> {
            (self.edge_evidence_fn)(self.ctx, from, to)
        }

        /// #1924/#1925 (epic #1906): the evidence-FILTERED counterpart of
        /// `callees_of` -- see `CodeGraph::callees_of_filtered`'s doc
        /// comment for the exact filter contract and complexity guarantee.
        pub fn callees_of_filtered(&self, symbol: u32, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
            (self.callees_of_filtered_fn)(self.ctx, symbol, required_bits, forbidden_bits)
        }

        /// #1924/#1925: the evidence-FILTERED counterpart of `callers_of`
        /// -- see `CodeGraph::callers_of_filtered`'s doc comment.
        pub fn callers_of_filtered(&self, symbol: u32, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
            (self.callers_of_filtered_fn)(self.ctx, symbol, required_bits, forbidden_bits)
        }

        /// #1924/#1925: the evidence-FILTERED counterpart of
        /// `reachable_from` -- see `CodeGraph::reachable_from_filtered`'s
        /// doc comment.
        pub fn reachable_from_filtered(&self, roots: &[u32], max_depth: usize, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
            (self.reachable_from_filtered_fn)(self.ctx, roots, max_depth, required_bits, forbidden_bits)
        }

        /// #1924/#1925: the evidence-FILTERED counterpart of
        /// `reachable_to` -- see `CodeGraph::reachable_to_filtered`'s doc
        /// comment.
        pub fn reachable_to_filtered(&self, targets: &[u32], max_depth: usize, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
            (self.reachable_to_filtered_fn)(self.ctx, targets, max_depth, required_bits, forbidden_bits)
        }

        /// #1924/#1925: the evidence-FILTERED counterpart of `strongly_
        /// connected_components` -- see `CodeGraph::strongly_connected_
        /// components_filtered`'s doc comment.
        pub fn strongly_connected_components_filtered(&self, required_bits: u16, forbidden_bits: u16) -> Vec<Vec<u32>> {
            (self.strongly_connected_components_filtered_fn)(self.ctx, required_bits, forbidden_bits)
        }

        /// #1953: the evidence-FILTERED counterpart of
        /// `shortest_path_to_any` -- see `CodeGraph::shortest_path_to_any_
        /// filtered`'s doc comment.
        pub fn shortest_path_to_any_filtered(&self, from: u32, targets: &[u32], max_depth: usize, required_bits: u16, forbidden_bits: u16) -> Option<Vec<u32>> {
            (self.shortest_path_to_any_filtered_fn)(self.ctx, from, targets, max_depth, required_bits, forbidden_bits)
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

            // Bug #1901: `reachable_to` must delegate to `CodeGraph::reachable_to`
            // exactly like `reachable_from` above -- queried from D backward
            // over the SAME fixture (A->B, A->C, B->D, C->D, D->A cycle),
            // it must find every transitive caller.
            let mut via_handle_to = handle.reachable_to(&[d], 100);
            via_handle_to.sort_unstable();
            let mut via_graph_to = graph.reachable_to(&[d], 100);
            via_graph_to.sort_unstable();
            assert_eq!(via_handle_to, via_graph_to);
            assert_eq!(via_handle_to, vec![a, b, c, d]);
            assert_eq!(handle.reachable_to(&[d], 0), vec![d]);

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

            assert_eq!(handle.resolve_symbol(a), Some(graph.resolve_symbol(a)));
            assert_eq!(handle.resolve_string(foo), Some("Foo"));
            assert_eq!(handle.resolve_string(foo), Some(graph.resolve_string(foo)));
        }

        /// D2 fix (dual-review Critical): the CENTRAL discriminating test.
        /// `d` is marked referenced (simulating the AC6 ladder marking it
        /// from the RAW pre-cap candidate list) but has ZERO entries in
        /// the CSR candidates arena (simulating the ladder capping its
        /// only edge away) -- so `callers_of(d)` is empty, exactly the
        /// blind spot D2 is about. `is_symbol_referenced`/
        /// `is_definitely_dead_code`, reached ONLY through `GraphHandle`
        /// (the surface `analyze_graph` actually receives), must still
        /// report it correctly.
        #[test]
        fn is_symbol_referenced_and_is_definitely_dead_code_survive_ladder_capping_via_the_handle() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
            let a = builder.intern_symbol(make_symbol_id(1, 0));
            let d = builder.intern_symbol(make_symbol_id(1, 1));
            let dead = builder.intern_symbol(make_symbol_id(1, 2));
            builder.add_reference(a, 1, 1, 0, &[]);
            builder.mark_referenced(d);
            builder.set_completeness(crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert!(handle.callers_of(d).is_empty(), "fixture sanity: d has zero POST-CAP callers");
            assert!(handle.is_symbol_referenced(d), "the pre-cap referenced bit must be reachable through the handle");
            assert_eq!(
                handle.is_definitely_dead_code(d),
                Some(false),
                "a referenced symbol must never be reported dead through the handle, even with zero POST-CAP callers"
            );

            // `dead` was never marked referenced at all, but the graph is
            // IndexBudgetExceeded -- the strongest dead-code tier must
            // stay suppressed (None), never a confident Some(true).
            assert_eq!(
                handle.is_definitely_dead_code(dead),
                None,
                "an unreferenced symbol under a degraded build must be suppressed through the handle too"
            );
        }

        /// Defect 2 (ADR-002 GraphHandle FFI fix): `resolve_symbol`/
        /// `resolve_string` must return `None` for an out-of-range id
        /// instead of panicking -- a panic here happens inside a HOST
        /// thunk called back FROM the dylib, which is UB (crosses the
        /// dylib boundary a second time before the dylib's own
        /// catch_unwind around analyze_graph could ever intercept it).
        #[test]
        fn resolve_symbol_and_resolve_string_return_none_for_out_of_range_ids_never_panic() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
            builder.intern_symbol(make_symbol_id(1, 0));
            builder.intern_string("Foo");
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(handle.resolve_symbol(u32::MAX), None, "out-of-range dense id must return None, never panic");
            assert_eq!(handle.resolve_string(u32::MAX), None, "out-of-range string id must return None, never panic");
        }

        /// Story #1792 (S3, AC4): "Each Symbol carries a short cached
        /// signature line captured at extraction" -- already true of
        /// `CodeGraph::signature_for` since #1787's S2 (AC6 step 1), but
        /// AC4 requires "cross-file captioning WITHOUT re-parsing", which
        /// means a graph-mode evaluator (the only consumer that ever
        /// crosses the dylib boundary) must be able to reach it. Before
        /// this fix, `GraphHandle` exposed no such accessor at all --
        /// `analyze_graph` had no way to caption a cross-file symbol
        /// without adding it to the RefineSet, which is exactly the blind
        /// spot AC3/AC4 exist to close. Must return `None` for a symbol
        /// with no cached signature (never panic, never fabricate one) and
        /// for an out-of-range dense id, mirroring `resolve_string`'s exact
        /// contract.
        #[test]
        fn signature_for_delegates_to_the_real_graph_and_returns_none_when_absent_or_out_of_range() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
            let with_sig = builder.intern_symbol(make_symbol_id(1, 0));
            let without_sig = builder.intern_symbol(make_symbol_id(1, 1));
            builder.add_signature(with_sig, "run()".to_string());
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(handle.signature_for(with_sig), Some("run()"));
            assert_eq!(handle.signature_for(with_sig), graph.signature_for(with_sig));
            assert_eq!(handle.signature_for(without_sig), None, "a symbol with no cached signature must return None, never panic or fabricate one");
            assert_eq!(handle.signature_for(u32::MAX), None, "an out-of-range dense id must return None, never panic");
        }

        /// Bug #1900 (epic #1906 P5): `location_for` must be reachable
        /// through the ONLY surface a graph-mode evaluator ever receives,
        /// delegating byte-for-byte to `CodeGraph::location_for`. `RED
        /// against unmodified code`: `GraphHandle` has no `location_for`
        /// method yet, so this fails to compile.
        #[test]
        fn location_for_delegates_to_the_real_graph_and_returns_none_when_absent() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
            let with_location = builder.intern_symbol(make_symbol_id(6, 0));
            let without_location = builder.intern_symbol(make_symbol_id(6, 1));
            let file_string_id = builder.intern_string("com/example/Handle.java");
            builder.add_location(with_location, file_string_id, 9);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(handle.location_for(with_location), Some(("com/example/Handle.java", 9)));
            assert_eq!(handle.location_for(with_location), graph.location_for(with_location));
            assert_eq!(handle.location_for(without_location), None, "a symbol with no location must return None, never fabricate one");
        }

        /// Bug #1900: `declaration_kind`/`visibility_of` must be reachable
        /// through `GraphHandle` and agree EXACTLY with the values
        /// `CodeGraph::is_definitely_dead_code` consults internally for the
        /// SAME dense id -- an evaluator implementing the documented
        /// "unwired components" use case for a specific declaration kind
        /// currently cannot filter by kind at all. `RED against unmodified
        /// code`: neither method exists on `GraphHandle` yet.
        #[test]
        fn declaration_kind_and_visibility_of_agree_with_what_is_definitely_dead_code_consults() {
            use crate::graph::extract::local_index::{DeclarationKind, Visibility};

            let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
            let unreferenced_private_method = builder.intern_symbol(make_symbol_id(7, 0));
            builder.add_kind(unreferenced_private_method, DeclarationKind::Method);
            builder.add_visibility(unreferenced_private_method, Visibility::Private);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(handle.declaration_kind(unreferenced_private_method), Some(DeclarationKind::Method));
            assert_eq!(handle.declaration_kind(unreferenced_private_method), graph.kind_for(unreferenced_private_method));
            assert_eq!(handle.visibility_of(unreferenced_private_method), Visibility::Private);
            assert_eq!(handle.visibility_of(unreferenced_private_method), graph.visibility_for(unreferenced_private_method));
            // The predicate these two values back: Method + Private on an
            // unreferenced symbol is exactly the one case that proves dead.
            assert_eq!(graph.is_definitely_dead_code(unreferenced_private_method), Some(true));
        }

        /// Bug #1900: `edge_reason` must be reachable through `GraphHandle`,
        /// delegating byte-for-byte to `CodeGraph::edge_reason`. `RED
        /// against unmodified code`: `GraphHandle` has no `edge_reason`
        /// method yet.
        #[test]
        fn edge_reason_delegates_to_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
            let caller = builder.intern_symbol(make_symbol_id(8, 0));
            let target = builder.intern_symbol(make_symbol_id(8, 1));
            builder.add_reference(caller, 8, 1, 0, &[Candidate::new(target, reasons::UNIQUE_NAME_IN_REPO)]);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(handle.edge_reason(caller, target), graph.edge_reason(caller, target));
            assert_eq!(handle.edge_reason(caller, target), Some(EdgeReason::SoleCandidate));
            assert_eq!(handle.edge_reason(caller, 999), None);
        }

        /// Bug #1900 (epic #1906 P2, review round 2): `edge_evidence` must be
        /// reachable through `GraphHandle`, delegating byte-for-byte to
        /// `CodeGraph::edge_evidence` -- the real evidence-bit accessor
        /// `edge_reason` alone cannot provide. `RED against unmodified
        /// code`: `GraphHandle` has no `edge_evidence` method yet.
        #[test]
        fn edge_evidence_delegates_to_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
            let caller = builder.intern_symbol(make_symbol_id(9, 0));
            let target = builder.intern_symbol(make_symbol_id(9, 1));
            builder.add_reference(caller, 9, 1, 0, &[Candidate::new(target, reasons::SAME_PACKAGE | reasons::ARITY_MATCH)]);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(handle.edge_evidence(caller, target), graph.edge_evidence(caller, target));
            assert_eq!(handle.edge_evidence(caller, target), Some(reasons::SAME_PACKAGE | reasons::ARITY_MATCH));
            assert_eq!(handle.edge_evidence(caller, 999), None, "a pair with no edge at all must report None through the handle too");
        }

        /// #1924/#1925: `callees_of_filtered`/`callers_of_filtered` must be
        /// reachable through `GraphHandle`, delegating byte-for-byte to
        /// their `CodeGraph` counterparts. `caller` has two callees:
        /// `matched` (RECEIVER_TYPE_MATCH only) and `mismatched`
        /// (RECEIVER_TYPE_MATCH | RECEIVER_TYPE_MISMATCH).
        #[test]
        fn callees_of_filtered_and_callers_of_filtered_delegate_to_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
            let caller = builder.intern_symbol(make_symbol_id(12, 0));
            let matched = builder.intern_symbol(make_symbol_id(12, 1));
            let mismatched = builder.intern_symbol(make_symbol_id(12, 2));
            builder.add_reference(caller, 12, 1, 0, &[Candidate::new(matched, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(
                caller,
                12,
                2,
                0,
                &[Candidate::new(mismatched, reasons::RECEIVER_TYPE_MATCH | reasons::RECEIVER_TYPE_MISMATCH)],
            );
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(
                handle.callees_of_filtered(caller, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH),
                graph.callees_of_filtered(caller, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH)
            );
            assert_eq!(
                handle.callees_of_filtered(caller, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH),
                vec![matched]
            );
            assert_eq!(
                handle.callers_of_filtered(matched, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH),
                graph.callers_of_filtered(matched, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH)
            );
            assert_eq!(
                handle.callers_of_filtered(matched, reasons::RECEIVER_TYPE_MATCH, reasons::RECEIVER_TYPE_MISMATCH),
                vec![caller]
            );
        }

        /// #1924/#1925: `reachable_from_filtered`/`reachable_to_filtered`/
        /// `strongly_connected_components_filtered` must be reachable
        /// through `GraphHandle`, delegating byte-for-byte to their
        /// `CodeGraph` counterparts. Reuses the diamond-plus-cycle fixture
        /// `reachable_from_and_shortest_path_and_scc_delegate_to_the_real_
        /// graph` above builds, tagging every edge `RECEIVER_TYPE_MATCH`.
        #[test]
        fn reachable_from_filtered_and_reachable_to_filtered_and_scc_filtered_delegate_to_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(5);
            let a = builder.intern_symbol(make_symbol_id(13, 0));
            let b = builder.intern_symbol(make_symbol_id(13, 1));
            let c = builder.intern_symbol(make_symbol_id(13, 2));
            let d = builder.intern_symbol(make_symbol_id(13, 3));
            builder.add_reference(a, 13, 1, 0, &[Candidate::new(b, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(a, 13, 2, 0, &[Candidate::new(c, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(b, 13, 3, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(c, 13, 4, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(d, 13, 5, 0, &[Candidate::new(a, reasons::RECEIVER_TYPE_MATCH)]);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            let mut via_handle = handle.reachable_from_filtered(&[a], 100, reasons::RECEIVER_TYPE_MATCH, 0);
            via_handle.sort_unstable();
            let mut via_graph = graph.reachable_from_filtered(&[a], 100, reasons::RECEIVER_TYPE_MATCH, 0);
            via_graph.sort_unstable();
            assert_eq!(via_handle, via_graph);
            assert_eq!(via_handle, vec![a, b, c, d]);

            let mut via_handle_to = handle.reachable_to_filtered(&[d], 100, reasons::RECEIVER_TYPE_MATCH, 0);
            via_handle_to.sort_unstable();
            let mut via_graph_to = graph.reachable_to_filtered(&[d], 100, reasons::RECEIVER_TYPE_MATCH, 0);
            via_graph_to.sort_unstable();
            assert_eq!(via_handle_to, via_graph_to);
            assert_eq!(via_handle_to, vec![a, b, c, d]);

            let mut via_handle_scc = handle.strongly_connected_components_filtered(reasons::RECEIVER_TYPE_MATCH, 0);
            let mut via_graph_scc = graph.strongly_connected_components_filtered(reasons::RECEIVER_TYPE_MATCH, 0);
            for comp in via_handle_scc.iter_mut().chain(via_graph_scc.iter_mut()) {
                comp.sort_unstable();
            }
            via_handle_scc.sort();
            via_graph_scc.sort();
            assert_eq!(via_handle_scc, via_graph_scc);
            assert_eq!(via_handle_scc.len(), 1, "a-b-d, a-c-d, d-a form one strongly connected component");

            // An empty mask must reach the SAME SET of nodes as the unfiltered
            // primitive -- compared as sorted sets, never raw Vec equality:
            // `callees_of_filtered` merges per-target evidence via a HashMap
            // internally (see `AdjacencyIndex::filtered_edges_of`), whose
            // iteration order is not guaranteed to match `callees_of`'s CSR
            // build order, even though the CONTENT is identical.
            let mut empty_mask = handle.reachable_from_filtered(&[a], 100, 0, 0);
            let mut unfiltered_from = handle.reachable_from(&[a], 100);
            empty_mask.sort_unstable();
            unfiltered_from.sort_unstable();
            assert_eq!(empty_mask, unfiltered_from);
        }

        /// #1953: `shortest_path_to_any_filtered` must be reachable through
        /// `GraphHandle`, delegating byte-for-byte to
        /// `CodeGraph::shortest_path_to_any_filtered`. Reuses the same
        /// diamond-plus-cycle fixture as the test above (every edge tagged
        /// RECEIVER_TYPE_MATCH), so filtering with an empty mask must
        /// reproduce the unfiltered result exactly.
        #[test]
        fn shortest_path_to_any_filtered_delegates_to_the_real_graph() {
            let mut builder = CodeGraphBuilder::with_candidate_capacity(5);
            let a = builder.intern_symbol(make_symbol_id(14, 0));
            let b = builder.intern_symbol(make_symbol_id(14, 1));
            let c = builder.intern_symbol(make_symbol_id(14, 2));
            let d = builder.intern_symbol(make_symbol_id(14, 3));
            builder.add_reference(a, 14, 1, 0, &[Candidate::new(b, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(a, 14, 2, 0, &[Candidate::new(c, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(b, 14, 3, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(c, 14, 4, 0, &[Candidate::new(d, reasons::RECEIVER_TYPE_MATCH)]);
            builder.add_reference(d, 14, 5, 0, &[Candidate::new(a, reasons::RECEIVER_TYPE_MATCH)]);
            let graph = builder.build();
            let handle = GraphHandle::from_graph(&graph);

            assert_eq!(
                handle.shortest_path_to_any_filtered(a, &[d], 100, reasons::RECEIVER_TYPE_MATCH, 0),
                graph.shortest_path_to_any_filtered(a, &[d], 100, reasons::RECEIVER_TYPE_MATCH, 0)
            );
            assert_eq!(
                handle.shortest_path_to_any_filtered(a, &[d], 100, reasons::RECEIVER_TYPE_MATCH, 0),
                handle.shortest_path_to_any(a, &[d], 100),
                "an empty forbidden mask (with the only evidence bit present required) must reproduce \
                 the unfiltered result over this fixture"
            );
            assert_eq!(
                handle.shortest_path_to_any_filtered(a, &[d], 1, reasons::RECEIVER_TYPE_MATCH, 0),
                None,
                "D is 2 hops away, beyond max_depth=1"
            );
        }
    }
}
