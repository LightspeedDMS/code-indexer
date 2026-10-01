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
    /// Bug #1926: dense symbol ids `is_definitely_
    /// dead_code` must treat as provably non-instantiable (a class's sole
    /// private no-arg constructor) -- a SEPARATE fact from `visibilities`,
    /// never a rewrite of it. See `CodeGraphBuilder`'s own field doc for
    /// the full rationale.
    non_instantiable_constructors: std::collections::HashSet<u32>,
    /// Dual-review defect M2 fix: CSR forward adjacency (callees), built
    /// ONCE here rather than re-scanned per query -- see `super::adjacency`
    /// module docs for why `callees_of`/`strongly_connected_components`
    /// were O(V*E) without this.
    forward_index: super::adjacency::AdjacencyIndex,
    /// M2 fix: CSR reverse adjacency (callers), same rationale as
    /// `forward_index`.
    reverse_index: super::adjacency::AdjacencyIndex,
    /// #1924/#1925: a SECOND reverse adjacency index that DOES carry
    /// per-occurrence evidence, built LAZILY (on the first call to a
    /// filtered CALLERS-direction primitive) rather than unconditionally
    /// at `from_parts` time -- see `reverse_evidence`'s own doc comment
    /// and `AdjacencyIndex::build_reverse_with_evidence`'s for the full
    /// rationale (the O(in-degree x caller out-degree) query cost this
    /// replaces, and the ~30.6 MB/analyze-child memory cost it now pays
    /// only when actually used).
    reverse_evidence_index: std::sync::OnceLock<super::adjacency::AdjacencyIndex>,
    /// #1924/#1925 (P3): test-only work-count instrumentation, incremented
    /// once per REAL invocation of the `reverse_evidence_index.get_or_init`
    /// closure -- proves the lazy build happens AT MOST ONCE across many
    /// `callers_of_filtered`/`reachable_to_filtered` queries on the same
    /// `CodeGraph`, without relying on wall-clock timing. Absent from a
    /// non-test build entirely (zero size, zero cost).
    #[cfg(test)]
    reverse_evidence_build_count: std::sync::atomic::AtomicUsize,
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
        non_instantiable_constructors: std::collections::HashSet<u32>,
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
            non_instantiable_constructors,
            forward_index,
            reverse_index,
            reverse_evidence_index: std::sync::OnceLock::new(),
            #[cfg(test)]
            reverse_evidence_build_count: std::sync::atomic::AtomicUsize::new(0),
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

    /// Bug #1926: true when `dense_symbol_id` was
    /// recorded as a class's sole private no-arg constructor -- the
    /// standard Java non-instantiable-utility-class idiom. Consulted by
    /// `is_definitely_dead_code` ALONGSIDE (never instead of) the ordinary
    /// visibility check: this never changes what `visibility_for` reports
    /// for the same symbol, which stays truthfully `Private`.
    pub fn is_non_instantiable_constructor(&self, dense_symbol_id: u32) -> bool {
        self.non_instantiable_constructors.contains(&dense_symbol_id)
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
    ///
    /// Bug #1926: a class's sole, no-arg, private
    /// constructor (`is_non_instantiable_constructor`) is the standard Java
    /// idiom for an intentionally non-instantiable utility class --
    /// "unreferenced" is the INTENDED state there, not evidence of dead
    /// code, so it is excluded from the `Some(true)` verdict below even
    /// though it is genuinely `Private`. This is a SEPARATE fact from
    /// `Visibility` (which stays truthfully `Private` for such a
    /// constructor, unaffected by this exception) -- see `CodeGraphBuilder`'s
    /// own field doc comment for why a `Visibility` rewrite was rejected.
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
        if self.is_non_instantiable_constructor(dense_symbol_id) {
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

    /// #1924/#1925 (epic #1906): the evidence-FILTERED counterpart of
    /// `callees_of` -- every callee of `dense_symbol_id` reached by AT
    /// LEAST ONE edge OCCURRENCE whose OWN `edge_evidence` satisfies
    /// `(bits & required_bits) == required_bits && (bits & forbidden_bits)
    /// == 0` -- see `AdjacencyIndex::filtered_edges_of`'s own doc comment
    /// for why this checks each occurrence independently rather than
    /// merging a target's evidence across occurrences first (a real edge
    /// and a separate fabricated edge to the same target must not cancel
    /// each other out). `required_bits: 0, forbidden_bits: 0` reaches the
    /// same SET of targets as `callees_of`, DEDUPLICATED -- `callees_of`
    /// itself returns one entry PER OCCURRENCE (duplicates included) in
    /// raw CSR order, so do not assume identical `Vec` length or order
    /// between the two, only the same underlying target set. O(out-degree
    /// of `dense_symbol_id`) via `AdjacencyIndex::filtered_edges_of` -- ONE
    /// pass over the forward CSR slice, never a per-callee `edge_evidence`
    /// call (which would reintroduce the O(out-degree^2) shape Bug #1900
    /// M2 already fixed for the unfiltered primitive). This is what lets an
    /// evaluator (via `GraphHandle`) analyse the "strong subgraph" directly
    /// -- e.g. requiring `RECEIVER_TYPE_MATCH` and forbidding `RECEIVER_
    /// TYPE_MISMATCH` to drop the #1924/#1925 fabricated-receiver edges
    /// from its own reachability/SCC analysis, without this binder ever
    /// deleting them from the raw graph itself.
    pub fn callees_of_filtered(&self, dense_symbol_id: u32, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        self.forward_index.filtered_edges_of(dense_symbol_id, required_bits, forbidden_bits)
    }

    /// #1924/#1925: the evidence-FILTERED counterpart of `callers_of`, same
    /// per-occurrence/dedup contract as `callees_of_filtered` above.
    /// Delegates to `reverse_evidence()`'s own `filtered_edges_of` -- an
    /// index built LAZILY, on the FIRST call to this method or `reachable_
    /// to_filtered` on this `CodeGraph`, then reused for every subsequent
    /// filtered-callers query. O(in-degree of `dense_symbol_id`) per call
    /// after that first build (same complexity class as `callees_of_
    /// filtered`), replacing a prior implementation whose per-query cost
    /// scaled with each caller's own out-degree instead. See `reverse_
    /// evidence`'s own doc comment for why the lazy build (not an
    /// unconditional one, matching the forward index) is deliberate; see
    /// `code_graph_edge_tests.rs`'s perf-shape test for the work-count
    /// proof.
    pub fn callers_of_filtered(&self, dense_symbol_id: u32, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        self.reverse_evidence().filtered_edges_of(dense_symbol_id, required_bits, forbidden_bits)
    }

    /// #1924/#1925: builds (on first call) or returns the cached `reverse_
    /// evidence_index` -- a SECOND reverse adjacency index that DOES carry
    /// per-occurrence evidence, unlike `reverse_index` (which stays
    /// evidence-free, per Bug #1900 round 3's ~30.6 MB/analyze-child
    /// memory rationale, since `callers_of`/`reachable_to` never needed
    /// evidence at all). Built from THIS graph's own `references`/
    /// `candidates` -- the same source `forward_index`/`reverse_index`
    /// were built from in `from_parts` -- so it is byte-for-byte
    /// consistent with them, just computed lazily instead of eagerly.
    /// `OnceLock::get_or_init` is safe to call from `&self` (interior
    /// mutability): every caller of a filtered CALLERS-direction primitive
    /// on the same `CodeGraph` shares ONE build, never rebuilding per
    /// call -- the `#[cfg(test)]` increment below only ever runs inside
    /// the closure, i.e. only on the real first build.
    fn reverse_evidence(&self) -> &super::adjacency::AdjacencyIndex {
        self.reverse_evidence_index.get_or_init(|| {
            #[cfg(test)]
            self.reverse_evidence_build_count.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            super::adjacency::AdjacencyIndex::build_reverse_with_evidence(self.symbols.len(), &self.references, &self.candidates)
        })
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
#[path = "code_graph_tests.rs"]
mod tests;
#[cfg(test)]
#[path = "code_graph_edge_tests.rs"]
mod edge_tests;
