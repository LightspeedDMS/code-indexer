//! `CodeGraph` -- the immutable, query-only CSR graph produced by
//! `CodeGraphBuilder::build` (Story #1787, S2, AC5).
//!
//! Every query here is O(edges)-path-safe: `references()` and
//! `candidates_for()` return borrowed slices, `resolve_symbol()` returns a
//! `SymbolId` (a plain `u64`, Copy) by value, and `resolve_string()` returns
//! `&str` borrowed from the shared string table. None of these allocate.

use super::candidate::Candidate;
use super::reference::Reference;
use super::symbol_table::SymbolTable;
use crate::graph::bind::depth::BinderDepth;
use crate::graph::budget::{AnalysisCompleteness, ReferencedBits};
use crate::graph::extract::local_index::{DeclarationKind, Visibility};
use crate::graph::identity::SymbolId;
use crate::graph::string_table::StringTable;
use std::collections::HashMap;

/// Bug #1900 (epic #1906 P2): the single distinction `CodeGraph::edge_reason`
/// promises -- whether the `(from, to)` edge is backed by at least one call
/// site where `to` was the ONLY surviving candidate (`SoleCandidate`) or
/// every contributing call site offered several candidates
/// (`MultipleCandidates`). This is the exact mechanism the cycle-precision
/// bug (#1899) needs to filter a finding (an SCC, a reachability path) down
/// to a trustworthy CANDIDATE-SET SHAPE.
///
/// Review round 2 (BLOCKING P2): this tier is renamed from
/// `Unambiguous`/`Ambiguous` to `SoleCandidate`/`MultipleCandidates` because
/// it is, and always was, a COUNT of surviving candidates -- never a claim
/// about whether the evidence backing that candidate is actually TRUE.
/// `Unambiguous` invited exactly that misreading: a call site can have
/// exactly one surviving candidate that is still a fabrication (e.g. a
/// same-named `put(2 params)` landed on via `SAME_PACKAGE`/`ARITY_MATCH`
/// alone, with no `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO`), and this
/// enum alone cannot tell that apart from a provably-correct single
/// candidate. `edge_evidence` (below) is the accessor that carries the
/// REAL evidence bits for that judgment -- this enum answers a narrower,
/// honestly-named question: "how many candidates survived at the strongest
/// contributing call site", nothing more.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeReason {
    /// At least one reference resolving `from` to `to` had exactly one
    /// surviving candidate in its window.
    SoleCandidate,
    /// Every reference resolving `from` to `to` had more than one
    /// surviving candidate in its window.
    MultipleCandidates,
}

/// Immutable, query-only whole-repository code graph. The only way to
/// build one is `CodeGraphBuilder::build` -- there is no public
/// constructor here that could assemble an internally-inconsistent graph
/// (e.g. a `Reference`'s `cand_start`/`cand_len` window pointing outside
/// `candidates`).
pub struct CodeGraph {
    references: Vec<Reference>,
    candidates: Vec<Candidate>,
    strings: StringTable,
    symbols: SymbolTable,
    binder_depths: Vec<BinderDepth>,
    /// AC6: whole-build completeness state (see `crate::graph::budget`).
    completeness: AnalysisCompleteness,
    /// AC6 step 3: the decoupled per-symbol referenced-bit.
    referenced: ReferencedBits,
    /// AC6 step 1: per-symbol cached signature lines, dropped entirely on
    /// a budget-exceeded build.
    signatures: HashMap<u32, String>,
    /// Story #1835 (AC2): per-symbol declared `Visibility`, attached
    /// unconditionally (never dropped under budget pressure -- see
    /// `CodeGraphBuilder::visibilities`'s doc comment). A dense id absent
    /// here reads back as `Visibility::Unknown` via `visibility_for`.
    visibilities: HashMap<u32, Visibility>,
    /// Bug #1858: per-symbol declared `DeclarationKind`, attached
    /// unconditionally (never dropped under budget pressure, mirroring
    /// `visibilities` exactly -- see `CodeGraphBuilder::kinds`'s doc
    /// comment). A dense id absent here has no known kind at all -- see
    /// `kind_for`'s doc comment for why that MUST be treated as unproven,
    /// never as license to report a symbol dead.
    kinds: HashMap<u32, DeclarationKind>,
    /// Bug #1900 (epic #1906 P5): per-symbol DECLARATION location -- a
    /// `(file_string_id, line)` pair into the shared `strings` table below.
    /// See `location_for`'s doc comment for why this is keyed by the
    /// declaration's own file+line, never a `Reference`'s call-site
    /// coordinates.
    locations: HashMap<u32, (u32, u32)>,
    /// Dual-review defect M2 fix: CSR forward adjacency (callees), built
    /// ONCE here rather than re-scanned per query -- see `super::adjacency`
    /// module docs for why `callees_of`/`strongly_connected_components`
    /// were O(V*E) without this.
    forward_index: super::adjacency::AdjacencyIndex,
    /// M2 fix: CSR reverse adjacency (callers), same rationale as
    /// `forward_index`.
    reverse_index: super::adjacency::AdjacencyIndex,
}

impl CodeGraph {
    /// Crate-internal: called only by `CodeGraphBuilder::build`, which is
    /// the sole place that produces these eight parts together and keeps
    /// them consistent. Builds the M2 forward/reverse adjacency indices
    /// HERE, once, from the same `references`/`candidates`/`symbols` this
    /// constructor already receives -- no change to this function's
    /// public parameter list.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn from_parts(
        references: Vec<Reference>,
        candidates: Vec<Candidate>,
        strings: StringTable,
        symbols: SymbolTable,
        binder_depths: Vec<BinderDepth>,
        completeness: AnalysisCompleteness,
        referenced: ReferencedBits,
        signatures: HashMap<u32, String>,
        visibilities: HashMap<u32, Visibility>,
        kinds: HashMap<u32, DeclarationKind>,
        locations: HashMap<u32, (u32, u32)>,
    ) -> Self {
        let forward_index = super::adjacency::AdjacencyIndex::build_forward(symbols.len(), &references, &candidates);
        let reverse_index = super::adjacency::AdjacencyIndex::build_reverse(symbols.len(), &references, &candidates);
        CodeGraph {
            references,
            candidates,
            strings,
            symbols,
            binder_depths,
            completeness,
            referenced,
            signatures,
            visibilities,
            kinds,
            locations,
            forward_index,
            reverse_index,
        }
    }

    /// AC6: this build's whole-graph completeness state.
    pub fn completeness(&self) -> AnalysisCompleteness {
        self.completeness
    }

    /// AC6 step 3: true once ANY raw candidate (regardless of whether it
    /// survived AC6 step-2 capping into the CSR arena) named this dense
    /// symbol id as its target.
    pub fn is_symbol_referenced(&self, dense_symbol_id: u32) -> bool {
        self.referenced.is_referenced(dense_symbol_id)
    }

    /// Story #1835 (AC2/AC3): this symbol's declared `Visibility`, or
    /// `Visibility::Unknown` if the extractor never recorded one (no
    /// modifier evidence, an unsupported declaration shape, or a language
    /// with no extractor at all -- see `Visibility`'s own doc comment for
    /// why `Unknown` is always the safe default, never a restricted one).
    pub fn visibility_for(&self, dense_symbol_id: u32) -> Visibility {
        self.visibilities.get(&dense_symbol_id).copied().unwrap_or(Visibility::Unknown)
    }

    /// Bug #1858: this symbol's extracted `DeclarationKind`, or `None` if
    /// the extractor never recorded one. Unlike `visibility_for`, there is
    /// no safe non-`Option` default to return here: `DeclarationKind` has
    /// no `Unknown`/catch-all variant, and fabricating one (e.g. defaulting
    /// to `Method`) would silently misclassify an absent entry as a
    /// tracked-reference kind, letting `is_definitely_dead_code` report a
    /// symbol of truly unknown kind as `Some(true)` on no evidence at all --
    /// exactly the false-certainty failure mode this bug is about. `None`
    /// here must be read the same way `is_definitely_dead_code` already
    /// reads `Visibility::Unknown`: no evidence, so no confident verdict.
    pub fn kind_for(&self, dense_symbol_id: u32) -> Option<DeclarationKind> {
        self.kinds.get(&dense_symbol_id).copied()
    }

    /// Bug #1900 (epic #1906 P5): this symbol's DECLARATION file path and
    /// 1-based line, or `None` if extraction never recorded one. This is
    /// DELIBERATELY distinct from any `Reference`'s `file`/`line` fields
    /// (`Reference.file` is a one-way SHA-256-derived hash, not even
    /// resolvable to a path string, and in any case names a CALL SITE, not
    /// a declaration) -- every production graph-mode finding today ships a
    /// bare `name(N params)` string with no way to chase it to source; this
    /// is the accessor that closes that gap. Returns a `&str` borrowed from
    /// the shared string table, with a lifetime tied to `&self`, never an
    /// owned `String` -- mirrors `resolve_string`/`signature_for`'s exact
    /// contract.
    ///
    /// Review round 2 (BLOCKING P3): uses the CHECKED `try_resolve`, never
    /// the panicking `resolve`. `thunk_location_for_raw` (`csr::mod::handle`)
    /// is an FFI thunk reached from a dylib-supplied `GraphHandle` on
    /// caller-controlled input (a corrupt or hand-crafted `--graph-in` file
    /// whose `file_string_id` is outside the decoded string table) --
    /// ADR-002 Defect 2 exists precisely so a panic can never cross that
    /// boundary, exactly like `try_resolve_symbol`/`try_resolve_string`
    /// already do for every other GraphHandle-reachable resolution. Returns
    /// `None` on an out-of-range `file_string_id` rather than panicking.
    pub fn location_for(&self, dense_symbol_id: u32) -> Option<(&str, usize)> {
        let &(file_string_id, line) = self.locations.get(&dense_symbol_id)?;
        let path = self.strings.try_resolve(file_string_id)?;
        Some((path, line as usize))
    }

    /// Bug #1900: crate-internal UNRESOLVED counterpart to `location_for`,
    /// returning the raw `(file_string_id, line)` pair as stored rather
    /// than resolving `file_string_id` through the string table. Exists so
    /// `csr::wire`'s writer can serialize this section directly off the
    /// same ids `write_strings` already wrote, without re-interning or
    /// re-resolving a `&str` it would immediately have to look back up.
    pub(super) fn raw_location_for(&self, dense_symbol_id: u32) -> Option<(u32, u32)> {
        self.locations.get(&dense_symbol_id).copied()
    }

    /// AC6 step 1: this symbol's cached AC2 signature line, or `None` if
    /// either the symbol never had one or snippets were dropped under
    /// budget pressure.
    pub fn signature_for(&self, dense_symbol_id: u32) -> Option<&str> {
        self.signatures.get(&dense_symbol_id).map(|s| s.as_str())
    }

    /// AC4: "`BinderDepth` exposed per language on the graph". One entry
    /// per distinct language the binder saw when this graph was built.
    pub fn binder_depths(&self) -> &[BinderDepth] {
        &self.binder_depths
    }

    /// All references in the repository, in build order. Borrowed slice --
    /// safe on an O(edges) query path.
    pub fn references(&self) -> &[Reference] {
        &self.references
    }

    /// The candidate set for one reference, sliced from the SHARED arena
    /// via its `cand_start`/`cand_len` window. Borrowed slice -- safe on an
    /// O(edges) query path.
    ///
    /// Fails loud (Rule 13/15) with a clear message on an out-of-bounds
    /// window rather than a bare slice-index panic, since `Reference` has
    /// public fields and could in principle be constructed by a caller
    /// with values that never came from this graph.
    pub fn candidates_for(&self, reference: &Reference) -> &[Candidate] {
        let start = reference.cand_start as usize;
        let len = reference.cand_len as usize;
        let end = start.checked_add(len).unwrap_or_else(|| {
            panic!("Reference candidate window overflows usize: start={start}, len={len}")
        });
        self.candidates.get(start..end).unwrap_or_else(|| {
            panic!(
                "Reference candidate window [{start}..{end}) is out of bounds for this \
                 CodeGraph's candidate arena (len={}) -- the Reference must come from this \
                 same CodeGraph's own builder",
                self.candidates.len()
            )
        })
    }

    /// M2 fix: every callee (dense symbol id) of `dense_symbol_id`, via
    /// the precomputed CSR forward adjacency index built ONCE in
    /// `from_parts` -- O(out-degree), never a re-scan of `references()`.
    /// Borrowed slice, safe on an O(edges) query path.
    pub fn callees_index(&self, dense_symbol_id: u32) -> &[u32] {
        self.forward_index.edges_of(dense_symbol_id)
    }

    /// M2 fix: every caller (dense symbol id) of `dense_symbol_id`, via
    /// the precomputed CSR reverse adjacency index -- O(in-degree), never
    /// a re-scan of `references()`/`candidates()`.
    pub fn callers_index(&self, dense_symbol_id: u32) -> &[u32] {
        self.reverse_index.edges_of(dense_symbol_id)
    }

    /// Resolves a `Candidate`'s dense symbol id back to the real 64-bit
    /// `SymbolId`. Returned BY VALUE: `SymbolId` is a plain `u64` (Copy),
    /// so this allocates nothing even on an O(edges) query path.
    pub fn resolve_symbol(&self, dense_id: u32) -> SymbolId {
        self.symbols.resolve(dense_id)
    }

    /// Resolves an interned string id back to its text, borrowed from the
    /// shared string table -- never an owned `String`.
    pub fn resolve_string(&self, string_id: u32) -> &str {
        self.strings.resolve(string_id)
    }

    /// Checked counterpart to `resolve_symbol` (ADR-002 Defect 2 fix):
    /// `None` instead of a panic on an out-of-range `dense_id`. Used by the
    /// `GraphHandle` FFI accessor thunk, which must never let a panic cross
    /// the dylib boundary on caller-supplied input.
    pub fn try_resolve_symbol(&self, dense_id: u32) -> Option<SymbolId> {
        self.symbols.try_resolve(dense_id)
    }

    /// Checked counterpart to `resolve_string` (ADR-002 Defect 2 fix):
    /// `None` instead of a panic on an out-of-range `string_id`. Used by
    /// the `GraphHandle` FFI accessor thunk, which must never let a panic
    /// cross the dylib boundary on caller-supplied input.
    pub fn try_resolve_string(&self, string_id: u32) -> Option<&str> {
        self.strings.try_resolve(string_id)
    }

    /// AC7: total number of DISTINCT symbols interned in this graph --
    /// dense ids are contiguous `0..symbol_count()`, so this is what lets
    /// `super::ops::strongly_connected_components` enumerate every node
    /// without this module exposing the `SymbolTable` type itself.
    pub fn symbol_count(&self) -> usize {
        self.symbols.len()
    }

    /// AC7: total number of DISTINCT strings interned in this graph --
    /// mirrors `symbol_count` above for the string table.
    pub fn string_count(&self) -> usize {
        self.strings.len()
    }

    /// Reverse lookup: the dense id `symbol` was interned under in THIS
    /// graph, if any. Lets a caller holding a real 64-bit `SymbolId` (e.g.
    /// from `FileForBind::index`, outside this crate's CSR internals)
    /// query `is_symbol_referenced`/`is_definitely_dead_code`.
    pub fn dense_id_for(&self, symbol: SymbolId) -> Option<u32> {
        self.symbols.dense_id_of(symbol)
    }

    /// Bug #1833 fix + Story #1835 restoration: this graph originally
    /// carried NO visibility or entry-point evidence anywhere in the CSR
    /// arena. `AnalysisCompleteness::Complete` means "every file in this
    /// repo parsed without degradation"; it does NOT mean "no caller
    /// exists anywhere" -- a library's entire public API has zero IN-REPO
    /// callers by construction, since real callers are downstream
    /// projects outside this repo. The pre-#1833-fix code treated
    /// `Complete` + unreferenced as proof of death, which on a real
    /// library (jsoup-global, Bug #1833) flagged 1296/3147 symbols
    /// "definitely dead" -- including documented public API -- purely
    /// because nothing else in the SAME repo happened to call them.
    ///
    /// Story #1835 restores a real `Some(true)` tier on top of that fix,
    /// scoped to EXACTLY the one case that is decidable without
    /// whole-program analysis: a symbol whose declared `Visibility` is
    /// PROVABLY not externally visible (`Visibility::is_provably_not_
    /// externally_visible`, currently `Private` only -- see that type's
    /// doc comment in `graph::extract::local_index`) cannot be called from
    /// outside this repository BY DEFINITION, so "unreferenced" for it
    /// really does mean dead. Every other case -- `Public`/`Protected`
    /// (externally reachable) or `Unknown` (no modifier evidence, or a
    /// declaration shape/language the extractor does not classify) --
    /// stays `None`, exactly Bug #1833's conservative behavior. This
    /// intentionally does NOT chase reflection, service loaders,
    /// annotation-driven invocation, dependency injection, or JNI: those
    /// mechanisms can make even a `private` symbol reachable in ways
    /// static analysis cannot see, but they operate through Java's
    /// reflection API (`Class`/`Method`/`Field` with `setAccessible`),
    /// which bypasses the language's OWN compile-time visibility
    /// enforcement entirely -- there is no local syntactic signal in the
    /// callee's own declaration that a caller will do this, so no
    /// visibility-based rule could soundly detect it without either
    /// tracking every such call site symbolically (out of scope here,
    /// full data-flow analysis) or refusing to trust `private` at all
    /// (which would forgo the capability this story exists to restore).
    /// The risk is bounded to genuinely `private`-declared symbols only
    /// (never `Public`/`Protected`/`Unknown`), matching the story's
    /// explicit scope.
    ///
    /// `Some(false)` ("referenced, not dead") stays unconditional: a real
    /// inbound edge is positive evidence, never invalidated by budget
    /// pressure, an indexing gap, or visibility. `completeness` plays no
    /// role in this decision (and never has for the `Some(false)` case):
    /// a `Declaration`'s visibility is a per-file syntactic fact read
    /// directly off its own modifiers, independent of whether the whole
    /// repository indexed completely -- if extraction of a file panicked
    /// entirely, that file contributes zero declarations at all (see
    /// `repo_index`), so a `Declaration` existing in this graph at all
    /// already implies its own file's extraction succeeded far enough to
    /// read its modifiers.
    ///
    /// Bug #1858 soundness floor: `Field` and `Constant` declarations are
    /// deliberately never classified as definitely dead here. The current
    /// extractor does not emit reference edges for their reads, so absence
    /// of an inbound edge is not evidence that either declaration is dead.
    /// Extracting field/constant references is deliberate future work: it
    /// requires scope-aware handling of locals, parameters, implicit
    /// `this`-field reads, and static imports, and is outside this narrowing.
    pub fn is_definitely_dead_code(&self, dense_symbol_id: u32) -> Option<bool> {
        if self.is_symbol_referenced(dense_symbol_id) {
            return Some(false);
        }
        // Only Method and Type references are currently tracked well enough
        // for the visibility-based dead-code proof. Unknown kinds must remain
        // undecidable rather than defaulting to a tracked kind.
        if !matches!(self.kind_for(dense_symbol_id), Some(DeclarationKind::Method | DeclarationKind::Type)) {
            return None;
        }
        if self.visibility_for(dense_symbol_id).is_provably_not_externally_visible() {
            return Some(true);
        }
        None
    }

    /// Bug #1900 (epic #1906 P2): whether the `(from, to)` edge is backed
    /// by at least one call site where `to` was the reference's ONLY
    /// candidate (`Some(EdgeReason::SoleCandidate)`), every contributing
    /// call site offered several candidates
    /// (`Some(EdgeReason::MultipleCandidates)`), or `from` never targets
    /// `to` at all (`None`). See `EdgeReason`'s doc comment for why this is
    /// a COUNT-based tier only, never a truth/provenance claim -- use
    /// `edge_evidence` for the latter.
    ///
    /// Complexity (review round 2 correction): O(out-degree of `from`) via
    /// the precomputed forward adjacency index -- never an O(edges) scan of
    /// `references()`. This is cheap for what an evaluator actually does
    /// with it: querying a handful of edges along a PATH (a reachability
    /// hop chain, typically a few hops) or the members of ONE SCC. It is
    /// NOT cheap to call once per edge while annotating every edge in the
    /// graph -- doing that for every node's out-edges is O(sum of
    /// out-degree^2), not O(edges): a single hub with a large out-degree
    /// dominates that sum on its own (measured ~0.55ns/edge-pair; a 100k
    /// out-degree hub alone costs ~5.5s under that usage pattern).
    pub fn edge_reason(&self, from: u32, to: u32) -> Option<EdgeReason> {
        self.forward_index
            .ambiguous_for_edge(from, to)
            .map(|ambiguous| if ambiguous { EdgeReason::MultipleCandidates } else { EdgeReason::SoleCandidate })
    }

    /// Bug #1900 (epic #1906 P2, review round 2): the REAL evidence
    /// accessor `edge_reason` cannot provide -- the bitwise-OR of
    /// `graph::reasons::*` bits across every candidate that contributed the
    /// `(from, to)` edge. `None` when `from` never targets `to` at all;
    /// `Some(0)` when it does but no candidate ever set an evidence bit
    /// (an edge built from a bare `Candidate::new(sym, 0)`, which real
    /// binder output never produces but a hand-built graph could). This is
    /// what lets an evaluator require e.g. `RECEIVER_TYPE_MATCH` or
    /// `UNIQUE_NAME_IN_REPO` before trusting a hop, rather than trusting
    /// `edge_reason`'s candidate COUNT alone -- the fabricated-edge case
    /// the review proved (a same-named overload landed on by
    /// `SAME_PACKAGE`/`ARITY_MATCH` alone, reported `SoleCandidate` despite
    /// being wrong) is exactly what this closes: `edge_evidence` on that
    /// same pair carries no `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO`
    /// bit, so a caller checking for either can tell the two cases apart.
    ///
    /// Same complexity profile and caveat as `edge_reason` above: O(out-
    /// degree of `from`), cheap for a path or an SCC member scan, NOT for
    /// annotating every edge in the graph.
    pub fn edge_evidence(&self, from: u32, to: u32) -> Option<u16> {
        self.forward_index.evidence_for_edge(from, to)
    }

    /// Dual-review defect D1 fix: records that `repo_index::build_repo_graph`
    /// (or any other caller ABOVE the binder that knows about a gap the
    /// binder itself cannot see -- `max_files` truncation, a parse error,
    /// an extractor panic, an unreadable source file) dropped part of the
    /// repository from this build. Only takes effect while `completeness`
    /// is still `Complete`: this never "upgrades" a degraded graph back to
    /// a healthier-looking state, and never clobbers a MORE specific
    /// reason (e.g. the binder ladder's own `IndexBudgetExceeded`) with a
    /// less specific one -- the first-recorded degradation reason wins.
    ///
    /// Bug #1833: `completeness` no longer gates the strongest dead-code
    /// tier. `is_definitely_dead_code` suppresses `Some(true)`
    /// unconditionally now (see its doc comment above) because the graph
    /// carries no visibility/entry-point evidence to back that verdict
    /// regardless of how complete the indexing pass was. The recorded
    /// reason still matters for `completeness()`'s other consumers (e.g.
    /// `repo_index`'s own `fact_graph_complete`/budget-exceeded reporting).
    pub fn downgrade_completeness(&mut self, reason: AnalysisCompleteness) {
        if self.completeness == AnalysisCompleteness::Complete {
            self.completeness = reason;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::super::builder::CodeGraphBuilder;
    use super::super::candidate::Candidate;
    use super::EdgeReason;
    use crate::graph::bind::depth::BinderDepth;
    use crate::graph::identity::make_symbol_id;
    use crate::graph::reasons;

    /// End-to-end wiring test: build a small graph through the real
    /// builder, then verify every query surface (`references`,
    /// `candidates_for`, `resolve_symbol`, `resolve_string`,
    /// `is_ambiguous`/`is_unresolved` read through the graph) agrees with
    /// what was built -- not just each piece in isolation.
    #[test]
    fn graph_round_trips_references_candidates_symbols_and_strings() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);

        let foo_symbol = make_symbol_id(1, 0);
        let bar_symbol = make_symbol_id(1, 1);
        let foo_dense = builder.intern_symbol(foo_symbol);
        let bar_dense = builder.intern_symbol(bar_symbol);
        let foo_name = builder.intern_string("Foo");

        // Reference 0: ambiguous call resolved to two candidates.
        builder.add_reference(
            10,
            1,
            5,
            0,
            &[
                Candidate::new(foo_dense, reasons::SAME_FILE),
                Candidate::new(bar_dense, reasons::SAME_PACKAGE),
            ],
        );
        // Reference 1: exact, single candidate.
        builder.add_reference(11, 1, 6, 0, &[Candidate::new(foo_dense, reasons::UNIQUE_NAME_IN_REPO)]);
        // Reference 2: out-of-repo call, empty candidate set.
        builder.add_reference(12, 1, 7, 0, &[]);

        builder.set_binder_depths(vec![BinderDepth::new("java")]);

        let graph = builder.build();

        assert_eq!(graph.references().len(), 3);
        assert_eq!(graph.binder_depths(), &[BinderDepth::new("java")]);

        let ref0 = graph.references()[0];
        assert!(ref0.is_ambiguous());
        assert!(!ref0.is_unresolved());
        let ref0_candidates = graph.candidates_for(&ref0);
        assert_eq!(ref0_candidates.len(), 2);
        assert_eq!(graph.resolve_symbol(ref0_candidates[0].symbol()), foo_symbol);
        assert_eq!(graph.resolve_symbol(ref0_candidates[1].symbol()), bar_symbol);

        let ref1 = graph.references()[1];
        assert!(!ref1.is_ambiguous());
        assert!(!ref1.is_unresolved());
        assert_eq!(graph.candidates_for(&ref1).len(), 1);

        let ref2 = graph.references()[2];
        assert!(ref2.is_unresolved());
        assert!(graph.candidates_for(&ref2).is_empty());

        assert_eq!(graph.resolve_string(foo_name), "Foo");
    }

    /// AC7: `strongly_connected_components` (in `super::ops`) needs to
    /// enumerate every interned symbol's dense id from OUTSIDE this
    /// module, where `self.symbols` is private -- this accessor is the
    /// seam that lets it do so without exposing the `SymbolTable` type
    /// itself.
    #[test]
    fn symbol_count_reports_the_number_of_distinct_interned_symbols() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        builder.intern_symbol(make_symbol_id(1, 0));
        builder.intern_symbol(make_symbol_id(1, 1));
        // Interning the SAME symbol again must not inflate the count.
        builder.intern_symbol(make_symbol_id(1, 0));

        let graph = builder.build();
        assert_eq!(graph.symbol_count(), 2);
    }

    /// AC7: `graph::csr::wire::write_graph_file` (next) needs to enumerate
    /// every interned string from OUTSIDE this module (where `self.strings`
    /// is private) to serialize the mmap-handoff wire format -- this
    /// accessor is that seam, mirroring `symbol_count` above exactly.
    #[test]
    fn string_count_reports_the_number_of_distinct_interned_strings() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        builder.intern_string("Foo");
        builder.intern_string("Bar");
        // Interning the SAME string again must not inflate the count.
        builder.intern_string("Foo");

        let graph = builder.build();
        assert_eq!(graph.string_count(), 2);
    }

    /// AC6: `CodeGraphBuilder` records the completeness state, the
    /// decoupled referenced-bit, and a per-symbol cached signature line;
    /// `CodeGraph` surfaces all three as real query methods, plus a
    /// reverse `dense_id_for` lookup and the `is_definitely_dead_code`
    /// dead-code-tier gate AC6 requires.
    #[test]
    fn budget_outcome_fields_round_trip_through_the_builder() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let live_symbol = make_symbol_id(1, 0);
        let dead_symbol = make_symbol_id(1, 1);
        let live_dense = builder.intern_symbol(live_symbol);
        let dead_dense = builder.intern_symbol(dead_symbol);

        builder.mark_referenced(live_dense);
        builder.add_signature(live_dense, "run()".to_string());
        builder.set_completeness(crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);

        let graph = builder.build();

        assert_eq!(graph.completeness(), crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);
        assert!(graph.is_symbol_referenced(live_dense));
        assert!(!graph.is_symbol_referenced(dead_dense));
        assert_eq!(graph.signature_for(live_dense), Some("run()"));
        assert_eq!(graph.signature_for(dead_dense), None);
        assert_eq!(graph.dense_id_for(live_symbol), Some(live_dense));
        assert_eq!(graph.dense_id_for(make_symbol_id(9, 9)), None);

        // Referenced -> never dead, regardless of completeness.
        assert_eq!(graph.is_definitely_dead_code(live_dense), Some(false));
        // Unreferenced + IndexBudgetExceeded -> suppressed (None), never
        // a false "definitely dead" verdict.
        assert_eq!(graph.is_definitely_dead_code(dead_dense), None);
    }

    /// Dual-review defect D1 (Critical): the pre-fix guard was an
    /// allowlist-of-one (`== IndexBudgetExceeded`) where the story's own
    /// docs demand an allowlist of exactly ONE good state (`!= Complete`
    /// suppresses everything else). `RepoIndexIncomplete` (repo-level
    /// indexing gaps: `max_files` truncation, parse errors, extractor
    /// panics, unreadable source files -- see `repo_index::build_repo_graph`)
    /// is the DISCRIMINATING case a wrong `== IndexBudgetExceeded` guard
    /// would miss: it is non-`Complete` but not `IndexBudgetExceeded`,
    /// so the old guard fell through to `Some(true)` -- a confident
    /// "definitely dead" verdict from a partially-indexed repository.
    #[test]
    fn is_definitely_dead_code_suppresses_the_dead_code_tier_for_every_non_complete_state() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let dead_symbol = builder.intern_symbol(make_symbol_id(1, 0));
        builder.set_completeness(crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete);
        let graph = builder.build();

        assert_eq!(
            graph.is_definitely_dead_code(dead_symbol),
            None,
            "an unreferenced symbol in a RepoIndexIncomplete graph must be suppressed (None), \
             never a confident Some(true) 'definitely dead' verdict"
        );
    }

    /// `downgrade_completeness` must set the reason exactly once (from the
    /// default `Complete`) and never clobber an already-recorded, more
    /// specific reason with a later, less specific one.
    #[test]
    fn downgrade_completeness_sets_reason_once_but_never_clobbers_an_existing_one() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        builder.intern_symbol(make_symbol_id(1, 0));
        let mut graph = builder.build();
        assert_eq!(graph.completeness(), crate::graph::budget::AnalysisCompleteness::Complete);

        graph.downgrade_completeness(crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete);
        assert_eq!(graph.completeness(), crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete);

        // A second, different reason must NOT overwrite the first.
        graph.downgrade_completeness(crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);
        assert_eq!(
            graph.completeness(),
            crate::graph::budget::AnalysisCompleteness::RepoIndexIncomplete,
            "the first-recorded degradation reason must win"
        );
    }

    /// Defect 2 (ADR-002 GraphHandle FFI fix): `try_resolve_symbol`/
    /// `try_resolve_string` are the checked counterparts to
    /// `resolve_symbol`/`resolve_string` -- they must delegate to
    /// `SymbolTable::try_resolve`/`StringTable::try_resolve` and return
    /// `None` on an out-of-range id, never panic. The panicking
    /// `resolve_symbol`/`resolve_string` are unchanged and still covered by
    /// `graph_round_trips_references_candidates_symbols_and_strings` above.
    #[test]
    fn try_resolve_symbol_and_try_resolve_string_are_checked_never_panicking_counterparts() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let foo_symbol = make_symbol_id(1, 0);
        let foo_dense = builder.intern_symbol(foo_symbol);
        let foo_name_id = builder.intern_string("Foo");
        let graph = builder.build();

        assert_eq!(graph.try_resolve_symbol(foo_dense), Some(foo_symbol));
        assert_eq!(graph.try_resolve_symbol(u32::MAX), None, "an out-of-range dense id must return None, never panic");

        assert_eq!(graph.try_resolve_string(foo_name_id), Some("Foo"));
        assert_eq!(graph.try_resolve_string(u32::MAX), None, "an out-of-range string id must return None, never panic");
    }

    /// Bug #1833 AC1 (discriminating regression test -- MUST fail on
    /// unmodified code, not just on a contrived input): a `Complete` graph
    /// carries NO visibility or entry-point data anywhere in the CSR arena
    /// (`SymbolId`, `Candidate`, `Reference`, `Declaration` all lack any
    /// modifier field -- verified by inspection before writing this test).
    /// So an unreferenced symbol here is exactly the shape of a library's
    /// public API symbol on a real repo: zero in-repo callers BY
    /// CONSTRUCTION, not because it is provably unreachable. Reporting
    /// `Some(true)` ("definitely dead") for it is a false certainty the
    /// graph cannot back up -- confirmed live on jsoup-global (Bug #1833:
    /// 1296/3147 symbols wrongly flagged, including documented public API
    /// like `Connection.contentType`). A test using a symbol some OTHER
    /// signal proves private would pass today on the pre-fix code too and
    /// prove nothing; this one does not smuggle in any such signal.
    #[test]
    fn is_definitely_dead_code_does_not_claim_certainty_for_an_unreferenced_symbol_with_no_visibility_evidence() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let library_api_symbol = builder.intern_symbol(make_symbol_id(1, 0));
        // Completeness defaults to `Complete` -- the exact condition under
        // which the pre-fix code fell through to `Some(true)`.
        let graph = builder.build();

        assert_eq!(
            graph.is_definitely_dead_code(library_api_symbol),
            None,
            "an unreferenced symbol on a Complete graph must be reported as undecidable (None) \
             when the graph holds no evidence the symbol is unreachable from OUTSIDE the repo -- \
             claiming Some(true) here is exactly Bug #1833's false 'definitely dead' verdict"
        );
    }

    /// Story #1835 AC4 (RED against unmodified code -- `CodeGraphBuilder`
    /// has no `add_visibility` method yet, so this fails to compile): the
    /// CENTRAL discriminating test for the whole story. On a SINGLE
    /// `Complete` graph, an unreferenced PRIVATE symbol must yield
    /// `Some(true)` (a real, provable dead-code verdict) while an
    /// unreferenced PUBLIC symbol in that SAME graph must stay `None`
    /// (Bug #1833's conservative behavior, unchanged for anything the
    /// visibility bit cannot prove safe). A test exercising only one of
    /// the two directions would prove nothing -- the whole point of this
    /// story is that the function now tells them apart.
    #[test]
    fn is_definitely_dead_code_distinguishes_unreferenced_private_from_unreferenced_public_on_a_complete_graph() {
        use crate::graph::extract::local_index::{DeclarationKind, Visibility};

        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let unreferenced_private = builder.intern_symbol(make_symbol_id(1, 0));
        let unreferenced_public = builder.intern_symbol(make_symbol_id(1, 1));
        builder.add_visibility(unreferenced_private, Visibility::Private);
        builder.add_visibility(unreferenced_public, Visibility::Public);
        // Bug #1858: both symbols here represent methods, so they carry a
        // tracked-reference kind -- real production code always attaches
        // one (see `budget_bind::intern_declarations_and_attach_signatures`),
        // and without it `is_definitely_dead_code` now correctly stays
        // undecidable regardless of visibility.
        builder.add_kind(unreferenced_private, DeclarationKind::Method);
        builder.add_kind(unreferenced_public, DeclarationKind::Method);
        // Completeness defaults to `Complete`.
        let graph = builder.build();

        assert_eq!(
            graph.is_definitely_dead_code(unreferenced_private),
            Some(true),
            "an unreferenced PRIVATE symbol is provably unreachable from outside the repo -- \
             this is the true positive Bug #1833's fix gave up and this story restores"
        );
        assert_eq!(
            graph.is_definitely_dead_code(unreferenced_public),
            None,
            "an unreferenced PUBLIC symbol stays undecidable -- external callers are invisible \
             to this repo's graph by construction, exactly Bug #1833's jsoup Connection/Response case"
        );
    }

    /// Bug #1858: field reads are not represented by inbound reference edges,
    /// so an unreferenced private field must not be classified as definitely
    /// dead. Keep the unreferenced private method in the same test so this
    /// remains discriminating: tracked declaration kinds still get the dead
    /// verdict. Turn 7 (codex) proved this RED against unmodified code using
    /// only `add_visibility` -- `add_kind` did not exist yet. Turn 8 (claude)
    /// extends it in place with real `add_kind` calls now that the
    /// declaration-kind channel exists; this must STILL be red at this point
    /// (`is_definitely_dead_code` does not consult kind yet -- that is turn
    /// 9's job), for the identical reason as before.
    #[test]
    fn is_definitely_dead_code_does_not_claim_unreferenced_private_field_is_dead() {
        use crate::graph::extract::local_index::{DeclarationKind, Visibility};

        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        // Intended field: its source-level read cannot become an inbound edge
        // with the current extractor, so the graph sees no reference here.
        let private_field = builder.intern_symbol(make_symbol_id(1, 0));
        // Intended method: genuinely unreferenced and tracked by the graph.
        let private_method = builder.intern_symbol(make_symbol_id(1, 1));
        builder.add_visibility(private_field, Visibility::Private);
        builder.add_visibility(private_method, Visibility::Private);
        builder.add_kind(private_field, DeclarationKind::Field);
        builder.add_kind(private_method, DeclarationKind::Method);
        let graph = builder.build();

        assert_eq!(graph.is_definitely_dead_code(private_field), None);
        assert_eq!(graph.is_definitely_dead_code(private_method), Some(true));
    }

    /// Bug #1858: `kind_for` must report back exactly the `DeclarationKind`
    /// attached via `add_kind` on a normal (non-degraded) build -- the
    /// baseline round trip every other `kind_for` test builds on.
    #[test]
    fn kind_for_returns_the_declared_kind_after_a_normal_build() {
        use crate::graph::extract::local_index::DeclarationKind;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let field_symbol = builder.intern_symbol(make_symbol_id(1, 0));
        let method_symbol = builder.intern_symbol(make_symbol_id(1, 1));
        builder.add_kind(field_symbol, DeclarationKind::Field);
        builder.add_kind(method_symbol, DeclarationKind::Method);
        let graph = builder.build();

        assert_eq!(graph.kind_for(field_symbol), Some(DeclarationKind::Field));
        assert_eq!(graph.kind_for(method_symbol), Some(DeclarationKind::Method));
    }

    /// Bug #1858 safe-default contract, half 1: a genuinely BUDGET-EXCEEDED
    /// build must still retain declaration kinds, exactly like
    /// `visibilities` (never dropped like `signatures`) -- otherwise a
    /// degraded build would silently lose the evidence that keeps a
    /// tracked-reference kind (e.g. Method) eligible for its existing
    /// `Some(true)` verdict, an unrelated regression this bug must not
    /// introduce. `set_completeness` here simulates the degraded state
    /// directly on the builder, mirroring
    /// `budget_outcome_fields_round_trip_through_the_builder` above --
    /// `kinds` has no separate "drop under budget" code path to simulate
    /// (unlike `signatures`, which `bind_with_budget` explicitly skips
    /// writing), so retention is verified as a direct, unconditional
    /// consequence of `add_kind` always being called.
    #[test]
    fn kind_for_is_retained_under_a_simulated_budget_exceeded_build() {
        use crate::graph::extract::local_index::DeclarationKind;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let method_symbol = builder.intern_symbol(make_symbol_id(1, 0));
        builder.add_kind(method_symbol, DeclarationKind::Method);
        builder.set_completeness(crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded);
        let graph = builder.build();

        assert_eq!(
            graph.completeness(),
            crate::graph::budget::AnalysisCompleteness::IndexBudgetExceeded
        );
        assert_eq!(
            graph.kind_for(method_symbol),
            Some(DeclarationKind::Method),
            "declaration kind must survive a budget-exceeded build, mirroring visibility \
             retention, so a degraded build never loses the evidence a tracked-reference kind \
             needs to keep its existing dead-code verdict"
        );
    }

    /// Bug #1858 safe-default contract, half 2: a symbol with NO kind
    /// evidence at all (never passed to `add_kind`) must read back as
    /// `None`, never fabricate a kind. This is the entry-absent case,
    /// distinct from the budget-exceeded-but-present case above --
    /// `is_definitely_dead_code` must treat this exactly as "unproven",
    /// never as license to report `Some(true)`.
    #[test]
    fn kind_for_returns_none_for_a_symbol_with_no_kind_evidence() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let unknown_kind_symbol = builder.intern_symbol(make_symbol_id(1, 0));
        let graph = builder.build();

        assert_eq!(
            graph.kind_for(unknown_kind_symbol),
            None,
            "a symbol never passed to add_kind must read back as None, never a fabricated kind"
        );
    }

    /// Bug #1833 AC2/AC3/AC4: on a library-shaped graph, raw reference
    /// evidence remains queryable independently of the conservative
    /// definitely-dead verdict. The referenced symbol is still known live,
    /// while both unreferenced symbols are undecidable rather than falsely
    /// classified as dead. This makes `definitely_dead < unreferenced`
    /// structural for the current visibility-blind graph representation.
    #[test]
    fn library_graph_under_reports_dead_code_without_aliasing_raw_references() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let referenced_symbol = builder.intern_symbol(make_symbol_id(1, 0));
        let unreferenced_api_a = builder.intern_symbol(make_symbol_id(1, 1));
        let unreferenced_api_b = builder.intern_symbol(make_symbol_id(1, 2));
        builder.mark_referenced(referenced_symbol);

        let graph = builder.build();

        let unreferenced = (0..graph.symbol_count() as u32)
            .filter(|&dense_id| !graph.is_symbol_referenced(dense_id))
            .count();
        let definitely_dead = (0..graph.symbol_count() as u32)
            .filter(|&dense_id| graph.is_definitely_dead_code(dense_id) == Some(true))
            .count();

        assert_eq!(unreferenced, 2);
        assert_eq!(definitely_dead, 0);
        assert!(definitely_dead < unreferenced);

        // AC3: the public raw query still reports the actual reference bit.
        assert!(graph.is_symbol_referenced(referenced_symbol));
        assert!(!graph.is_symbol_referenced(unreferenced_api_a));
        assert!(!graph.is_symbol_referenced(unreferenced_api_b));
        // AC4: positive in-repo evidence remains Some(false).
        assert_eq!(graph.is_definitely_dead_code(referenced_symbol), Some(false));
        // The two queries are deliberately not synonyms for unreferenced API.
        assert_eq!(graph.is_definitely_dead_code(unreferenced_api_a), None);
        assert_eq!(graph.is_definitely_dead_code(unreferenced_api_b), None);
    }

    /// Bug #1900 (epic #1906 P2, inherited from #1899's AC): the CENTRAL
    /// discriminating test for `edge_reason`. `caller` has two outbound
    /// references: one resolved to a SINGLE surviving candidate
    /// (`unambiguous_target`), and one whose window held TWO surviving
    /// candidates (`ambiguous_target_a`/`_b`). A caller filtering a graph
    /// finding (e.g. an SCC or a reachability path) to trustworthy edges
    /// needs to tell these apart -- this is the exact mechanism the
    /// cycle-precision bug (#1899) asks for. `RED against unmodified code`:
    /// neither `CodeGraph::edge_reason` nor `EdgeReason` exist yet, so this
    /// fails to compile -- once implemented, a wrong implementation (e.g.
    /// always reporting `Ambiguous`, or never distinguishing by window
    /// size) would still fail these specific assertions.
    #[test]
    fn edge_reason_distinguishes_unambiguous_single_candidate_from_ambiguous_multi_candidate_edges() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
        let caller = builder.intern_symbol(make_symbol_id(1, 0));
        let unambiguous_target = builder.intern_symbol(make_symbol_id(1, 1));
        let ambiguous_target_a = builder.intern_symbol(make_symbol_id(1, 2));
        let ambiguous_target_b = builder.intern_symbol(make_symbol_id(1, 3));

        // Reference 0: caller -> unambiguous_target, exactly ONE candidate.
        builder.add_reference(caller, 1, 10, 0, &[Candidate::new(unambiguous_target, reasons::UNIQUE_NAME_IN_REPO)]);
        // Reference 1: caller -> {ambiguous_target_a, ambiguous_target_b}, TWO candidates.
        builder.add_reference(
            caller,
            1,
            11,
            0,
            &[
                Candidate::new(ambiguous_target_a, reasons::SAME_PACKAGE),
                Candidate::new(ambiguous_target_b, reasons::SAME_PACKAGE),
            ],
        );
        let graph = builder.build();

        assert_eq!(
            graph.edge_reason(caller, unambiguous_target),
            Some(EdgeReason::SoleCandidate),
            "a single-candidate reference window must report SoleCandidate"
        );
        assert_eq!(
            graph.edge_reason(caller, ambiguous_target_a),
            Some(EdgeReason::MultipleCandidates),
            "a multi-candidate reference window must report MultipleCandidates for every candidate in it"
        );
        assert_eq!(graph.edge_reason(caller, ambiguous_target_b), Some(EdgeReason::MultipleCandidates));
        assert_eq!(
            graph.edge_reason(caller, 999),
            None,
            "a pair with no edge at all must report None, never a fabricated tier"
        );
    }

    /// A single symbol pair reached by BOTH an ambiguous and an
    /// unambiguous reference must report `Unambiguous` -- real, positive
    /// single-target evidence from ONE call site is never invalidated by a
    /// separate, weaker call site that also happens to target the same
    /// symbol.
    #[test]
    fn edge_reason_prefers_unambiguous_when_the_same_pair_has_both_kinds_of_evidence() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(3);
        let caller = builder.intern_symbol(make_symbol_id(2, 0));
        let target = builder.intern_symbol(make_symbol_id(2, 1));
        let other = builder.intern_symbol(make_symbol_id(2, 2));

        // Reference 0: caller -> {target, other}, ambiguous.
        builder.add_reference(
            caller,
            2,
            5,
            0,
            &[Candidate::new(target, reasons::SAME_PACKAGE), Candidate::new(other, reasons::SAME_PACKAGE)],
        );
        // Reference 1: caller -> target, unambiguous.
        builder.add_reference(caller, 2, 6, 0, &[Candidate::new(target, reasons::UNIQUE_NAME_IN_REPO)]);
        let graph = builder.build();

        assert_eq!(
            graph.edge_reason(caller, target),
            Some(EdgeReason::SoleCandidate),
            "one sole-candidate call site is real evidence, regardless of a separate multi-candidate one"
        );
    }

    /// Bug #1900 (epic #1906 P2, review round 2 -- the CENTRAL discriminating
    /// test for the fabricated-edge fix, at the `CodeGraph` level): a
    /// `SoleCandidate` edge (per `edge_reason`) can still carry weak
    /// evidence only -- `edge_evidence` must report the candidate's REAL
    /// `reasons()` bitmask, never derive anything from the candidate count.
    /// Reproduces the review's own `java.util.Map.put` shape: a single
    /// surviving candidate resolved via `SAME_PACKAGE`/`ARITY_MATCH` alone,
    /// with no `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO`.
    #[test]
    fn edge_evidence_reports_weak_evidence_for_a_sole_candidate_edge() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
        let caller = builder.intern_symbol(make_symbol_id(5, 0));
        let fabricated_target = builder.intern_symbol(make_symbol_id(5, 1));

        // A single surviving candidate (edge_reason == SoleCandidate) whose
        // ONLY evidence is weak structural matching -- the exact shape the
        // review proved gets mislabeled by cand_len alone.
        builder.add_reference(
            caller,
            5,
            1,
            0,
            &[Candidate::new(fabricated_target, reasons::SAME_PACKAGE | reasons::ARITY_MATCH)],
        );
        let graph = builder.build();

        assert_eq!(
            graph.edge_reason(caller, fabricated_target),
            Some(EdgeReason::SoleCandidate),
            "fixture sanity: this is exactly the count-based tier a fabricated single candidate reports"
        );
        assert_eq!(
            graph.edge_evidence(caller, fabricated_target),
            Some(reasons::SAME_PACKAGE | reasons::ARITY_MATCH),
            "edge_evidence must report the candidate's REAL reasons bitmask, not a count-derived flag"
        );
        assert_eq!(
            graph.edge_evidence(caller, fabricated_target).unwrap() & reasons::RECEIVER_TYPE_MATCH,
            0,
            "the fabricated edge carries no RECEIVER_TYPE_MATCH bit -- a caller checking for \
             strong evidence can now tell this apart from a genuinely verified hop"
        );
    }

    /// Bug #1900 (review round 2, reviewer-relay finding): a single
    /// pre-combined candidate cannot distinguish "OR of every contributing
    /// occurrence" from "just returns the first/only match's reasons" -- an
    /// implementation that picked ONE candidate's reasons rather than
    /// OR-ing across occurrences would still pass the test above. This test
    /// uses TWO SEPARATE references from the same caller to the same
    /// target, each carrying a DIFFERENT, non-overlapping reason bit, and
    /// asserts `edge_evidence` reports the bitwise UNION of both -- the
    /// only way that union can appear is if both occurrences were actually
    /// combined.
    #[test]
    fn edge_evidence_ors_bits_across_two_separate_references_to_the_same_target() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(2);
        let caller = builder.intern_symbol(make_symbol_id(6, 0));
        let target = builder.intern_symbol(make_symbol_id(6, 1));

        // Reference 0: caller -> target, evidence bit A only.
        builder.add_reference(caller, 6, 1, 0, &[Candidate::new(target, reasons::SAME_PACKAGE)]);
        // Reference 1: a SEPARATE call site, caller -> target again, evidence bit B only.
        builder.add_reference(caller, 6, 2, 0, &[Candidate::new(target, reasons::UNIQUE_NAME_IN_REPO)]);
        let graph = builder.build();

        assert_eq!(
            graph.edge_evidence(caller, target),
            Some(reasons::SAME_PACKAGE | reasons::UNIQUE_NAME_IN_REPO),
            "edge_evidence must be the OR of BOTH occurrences' reason bits, not either one alone"
        );
    }

    #[test]
    fn edge_evidence_returns_none_for_a_pair_with_no_edge() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
        let caller = builder.intern_symbol(make_symbol_id(7, 0));
        let target = builder.intern_symbol(make_symbol_id(7, 1));
        builder.add_reference(caller, 7, 1, 0, &[Candidate::new(target, reasons::SAME_FILE)]);
        let graph = builder.build();

        assert_eq!(
            graph.edge_evidence(caller, 999),
            None,
            "a pair with no edge at all must report None, never a fabricated Some(0)"
        );
    }

    /// Bug #1900 (epic #1906 P2/P5): `location_for` must report a
    /// DECLARATION's own file+line -- captured via `add_location`,
    /// independent of any `Reference`'s call-site coordinates -- and must
    /// return `None`, never fabricate one, for a symbol with no recorded
    /// location. `RED against unmodified code`: `CodeGraphBuilder` has no
    /// `add_location` method yet, so this fails to compile.
    #[test]
    fn location_for_returns_the_declared_file_and_line_and_none_when_absent() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let with_location = builder.intern_symbol(make_symbol_id(4, 0));
        let without_location = builder.intern_symbol(make_symbol_id(4, 1));
        let file_string_id = builder.intern_string("com/example/Foo.java");
        builder.add_location(with_location, file_string_id, 42);
        let graph = builder.build();

        assert_eq!(
            graph.location_for(with_location),
            Some(("com/example/Foo.java", 42)),
            "location_for must resolve the interned file path and the exact recorded line"
        );
        assert_eq!(
            graph.location_for(without_location),
            None,
            "a symbol never passed to add_location must return None, never a fabricated location"
        );
    }

    /// Bug #1900 (epic #1906 P2, review round 2 -- BLOCKING P3): a corrupt
    /// `file_string_id` (out of range for this graph's string table) must
    /// make `location_for` return `None`, never panic. `thunk_location_for_
    /// raw` is an FFI thunk reached from a dylib-supplied `GraphHandle` on
    /// caller-controlled input; ADR-002 Defect 2 exists precisely so a
    /// panic can never cross that boundary, exactly like
    /// `try_resolve_symbol`/`try_resolve_string` already guarantee for
    /// every other GraphHandle-reachable resolution. This builds the
    /// corrupt state directly via `add_location` (bypassing `intern_string`
    /// entirely) -- the same shape a corrupt `--graph-in` file would
    /// produce if it ever slipped past `read_locations`'s own wire-level
    /// validation (see `wire.rs`). `RED against the pre-fix code`: the old
    /// `self.strings.resolve(file_string_id)` panics here instead of
    /// returning `None`.
    #[test]
    fn location_for_returns_none_instead_of_panicking_for_a_corrupt_file_string_id() {
        let mut builder = CodeGraphBuilder::with_candidate_capacity(0);
        let symbol = builder.intern_symbol(make_symbol_id(9, 0));
        const OUT_OF_RANGE_STRING_ID: u32 = 999;
        builder.add_location(symbol, OUT_OF_RANGE_STRING_ID, 1);
        let graph = builder.build();

        assert_eq!(graph.location_for(symbol), None, "an out-of-range file_string_id must return None, never panic");
    }
}
