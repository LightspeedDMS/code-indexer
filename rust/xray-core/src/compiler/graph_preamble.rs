//! Issue #1934: the graph-mode evaluator mirror text -- `GraphHandle`/
//! `FactsHandle`/`UserFact`/`GraphResult`/`FileContext`/the
//! `graph::reasons::*` evidence-bit constants -- extracted verbatim out of
//! `compiler.rs` (pure move -- no behaviour change, see the module doc
//! comment on `super`). These constants are STRING LITERALS compiled
//! verbatim into every graph-mode evaluator artifact; their content must
//! never be touched by a refactor -- only the Rust-level visibility of
//! `GRAPH_EPILOGUE`/`GRAPH_REFINE_EPILOGUE` changes here (bumped from
//! private to `pub(crate)` so sibling modules in the new `compiler` tree,
//! and the split test files, can still reach them -- this has zero effect
//! on the compiled evaluator, since Rust visibility is a compile-time-only
//! concept that never appears in generated code). The legacy-mode
//! PREAMBLE/EPILOGUE live in the sibling `preamble` module.

/// Story #1787 AC8 / ADR-002: mirrors the REAL `GraphHandle`
/// (`graph::csr::handle`) type for a graph-mode evaluator's compiled
/// source. Appended to the COMMON `PREAMBLE` above (which still supplies
/// `OwnedNode`/`EvalFinding`/`debug_log`, shared by both modes) -- never a
/// standalone replacement for it. Structurally parity-checked against the
/// real type by `preamble_ac18_parity.rs`, exactly like `PREAMBLE`'s
/// `OwnedNode`/`EvalFinding` mirror. Per ADR-002, `CodeGraph`'s own
/// evolving CSR/StringTable/SymbolTable internals are NEVER mirrored here
/// -- `GraphHandle` carries only an opaque context pointer plus fixed
/// accessor function pointers, bound by the HOST at construction time and
/// passed by reference; each accessor method BODY below must stay
/// byte-identical to `graph::csr::handle::GraphHandle`'s real one (it just
/// invokes the stored fn pointer, nothing more).
///
/// Built up incrementally across several constants (this one holds the
/// struct plus its first 3 accessor methods; `GRAPH_PREAMBLE_EXTRA_2`/`_3`
/// hold the rest) purely to keep each source edit's method count small;
/// `assemble_graph_evaluator_source` concatenates all of them in order.
pub(crate) const GRAPH_PREAMBLE_EXTRA_1: &str = r#"
pub type SymbolId = u64;

use std::marker::PhantomData;

type CtxPtr = *const ();

#[derive(Clone, Copy)]
pub struct GraphHandle<'graph> {
    ctx: CtxPtr,
    callees_of_fn: fn(*const (), u32) -> Vec<u32>,
    callers_of_fn: fn(*const (), u32) -> Vec<u32>,
    reachable_from_fn: fn(*const (), &[u32], usize) -> Vec<u32>,
    reachable_to_fn: fn(*const (), &[u32], usize) -> Vec<u32>,
    shortest_path_to_any_fn: fn(*const (), u32, &[u32], usize) -> Option<Vec<u32>>,
    strongly_connected_components_fn: fn(*const ()) -> Vec<Vec<u32>>,
    resolve_symbol_fn: fn(*const (), u32) -> Option<u64>,
    resolve_string_raw_fn: fn(*const (), u32) -> Option<(*const u8, usize)>,
    is_symbol_referenced_fn: fn(*const (), u32) -> bool,
    is_definitely_dead_code_fn: fn(*const (), u32) -> Option<bool>,
    signature_for_raw_fn: fn(*const (), u32) -> Option<(*const u8, usize)>,
    symbol_count_fn: fn(*const ()) -> usize,
    dense_id_for_fn: fn(*const (), u64) -> Option<u32>,
    location_for_raw_fn: fn(*const (), u32) -> Option<(*const u8, usize, usize)>,
    declaration_kind_fn: fn(*const (), u32) -> Option<DeclarationKind>,
    visibility_of_fn: fn(*const (), u32) -> Visibility,
    edge_reason_fn: fn(*const (), u32, u32) -> Option<EdgeReason>,
    edge_evidence_fn: fn(*const (), u32, u32) -> Option<u16>,
    callees_of_filtered_fn: fn(*const (), u32, u16, u16) -> Vec<u32>,
    callers_of_filtered_fn: fn(*const (), u32, u16, u16) -> Vec<u32>,
    reachable_from_filtered_fn: fn(*const (), &[u32], usize, u16, u16) -> Vec<u32>,
    reachable_to_filtered_fn: fn(*const (), &[u32], usize, u16, u16) -> Vec<u32>,
    strongly_connected_components_filtered_fn: fn(*const (), u16, u16) -> Vec<Vec<u32>>,
    _graph: PhantomData<&'graph ()>,
}

impl<'graph> GraphHandle<'graph> {
    pub fn callees_of(&self, symbol: u32) -> Vec<u32> {
        (self.callees_of_fn)(self.ctx, symbol)
    }

    pub fn callers_of(&self, symbol: u32) -> Vec<u32> {
        (self.callers_of_fn)(self.ctx, symbol)
    }

    pub fn reachable_from(&self, roots: &[u32], max_depth: usize) -> Vec<u32> {
        (self.reachable_from_fn)(self.ctx, roots, max_depth)
    }

    pub fn reachable_to(&self, targets: &[u32], max_depth: usize) -> Vec<u32> {
        (self.reachable_to_fn)(self.ctx, targets, max_depth)
    }
"#;

/// Continues `GRAPH_PREAMBLE_EXTRA_1` -- see its doc comment. Holds
/// `GraphHandle`'s next 3 accessor methods.
pub(crate) const GRAPH_PREAMBLE_EXTRA_2: &str = r#"
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
"#;

/// Continues `GRAPH_PREAMBLE_EXTRA_1`/`_2` -- see the first's doc comment.
/// Closes `GraphHandle`'s impl block with its final accessor
/// (`resolve_string`), then mirrors the REAL `UserFact` and `FactsHandle`
/// types (`graph::user_facts`) the same way: `FactsHandle` carries only an
/// opaque context pointer plus accessor function pointers (Story #1785:
/// `for_symbol_fn` and `for_custom_fn`), never `FactIndex`'s internal
/// `HashMap`/`StringTable` layout (the same ADR-002 principle extended
/// from `CodeGraph` to `FactIndex`).
pub(crate) const GRAPH_PREAMBLE_EXTRA_3: &str = r#"
    pub fn resolve_string(&self, string_id: u32) -> Option<&str> {
        let (ptr, len) = (self.resolve_string_raw_fn)(self.ctx, string_id)?;
        Some(unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) })
    }

    pub fn is_symbol_referenced(&self, dense_id: u32) -> bool {
        (self.is_symbol_referenced_fn)(self.ctx, dense_id)
    }

    pub fn is_definitely_dead_code(&self, dense_id: u32) -> Option<bool> {
        (self.is_definitely_dead_code_fn)(self.ctx, dense_id)
    }

    pub fn signature_for(&self, dense_id: u32) -> Option<&str> {
        let (ptr, len) = (self.signature_for_raw_fn)(self.ctx, dense_id)?;
        Some(unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) })
    }

    pub fn location_for(&self, dense_id: u32) -> Option<(&str, usize)> {
        let (ptr, len, line) = (self.location_for_raw_fn)(self.ctx, dense_id)?;
        Some((unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) }, line))
    }

    pub fn declaration_kind(&self, dense_id: u32) -> Option<DeclarationKind> {
        (self.declaration_kind_fn)(self.ctx, dense_id)
    }

    pub fn visibility_of(&self, dense_id: u32) -> Visibility {
        (self.visibility_of_fn)(self.ctx, dense_id)
    }

    pub fn edge_reason(&self, from: u32, to: u32) -> Option<EdgeReason> {
        (self.edge_reason_fn)(self.ctx, from, to)
    }

    pub fn edge_evidence(&self, from: u32, to: u32) -> Option<u16> {
        (self.edge_evidence_fn)(self.ctx, from, to)
    }

    pub fn callees_of_filtered(&self, symbol: u32, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        (self.callees_of_filtered_fn)(self.ctx, symbol, required_bits, forbidden_bits)
    }

    pub fn callers_of_filtered(&self, symbol: u32, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        (self.callers_of_filtered_fn)(self.ctx, symbol, required_bits, forbidden_bits)
    }

    pub fn reachable_from_filtered(&self, roots: &[u32], max_depth: usize, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        (self.reachable_from_filtered_fn)(self.ctx, roots, max_depth, required_bits, forbidden_bits)
    }

    pub fn reachable_to_filtered(&self, targets: &[u32], max_depth: usize, required_bits: u16, forbidden_bits: u16) -> Vec<u32> {
        (self.reachable_to_filtered_fn)(self.ctx, targets, max_depth, required_bits, forbidden_bits)
    }

    pub fn strongly_connected_components_filtered(&self, required_bits: u16, forbidden_bits: u16) -> Vec<Vec<u32>> {
        (self.strongly_connected_components_filtered_fn)(self.ctx, required_bits, forbidden_bits)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeclarationKind {
    Type,
    Method,
    Field,
    Constant,
    Package,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Visibility {
    Public,
    Protected,
    Private,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EdgeReason {
    SoleCandidate,
    MultipleCandidates,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserFact {
    pub kind: String,
    pub line: usize,
    pub message: String,
    pub custom_key: Option<String>,
}

#[derive(Clone, Copy)]
pub struct FactsHandle<'facts> {
    ctx: *const (),
    for_symbol_fn: fn(*const (), u64) -> Vec<UserFact>,
    for_custom_fn: fn(*const (), &str) -> Vec<UserFact>,
    _facts: std::marker::PhantomData<&'facts ()>,
}

impl<'facts> FactsHandle<'facts> {
    pub fn for_symbol(&self, symbol: SymbolId) -> Vec<UserFact> {
        (self.for_symbol_fn)(self.ctx, symbol)
    }

    pub fn for_custom(&self, name: &str) -> Vec<UserFact> {
        (self.for_custom_fn)(self.ctx, name)
    }
}
"#;

/// Concludes `GRAPH_PREAMBLE_EXTRA_1`/`_2`/`_3` (see the first's doc
/// comment): mirrors the REAL `ReduceFinding`/`GraphResult` types
/// (`graph::analyze::result`) that `analyze_graph` constructs and returns
/// -- plain data structs, no accessor methods, since evaluator code
/// constructs these values directly rather than querying them through a
/// handle.
pub(crate) const GRAPH_PREAMBLE_EXTRA_4: &str = r#"
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ReduceFinding {
    pub pattern: String,
    pub message: String,
    pub involved: Vec<SymbolId>,
    pub signatures: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GraphResult {
    pub findings: Vec<ReduceFinding>,
    pub refine: Vec<SymbolId>,
}
"#;

/// Story #1792 (S3, AC1): mirrors the REAL `FileContext` type
/// (`graph::refine::FileContext`) -- the per-file host context an OPTIONAL
/// `refine` callback receives alongside the file's `OwnedNode` and the
/// whole-graph handles. Unlike `CodeGraph`/`FactIndex`, `FileContext` is
/// small, plain data with no internal collection layout to evolve, so it is
/// mirrored directly (like `EvalFinding`) rather than exposed through an
/// opaque handle.
pub(crate) const GRAPH_PREAMBLE_EXTRA_5: &str = r#"
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileContext {
    pub file: String,
}
"#;

/// Bug #1900 (epic #1906 P2, review round 2): mirrors the REAL
/// `graph::reasons::*` bit constants (`reasons.rs`) so evaluator code that
/// calls `GraphHandle::edge_evidence` has something to test the returned
/// `u16` against by NAME -- "a `u16` nobody can interpret is not
/// observability". Structurally parity-checked against the real constants
/// by `preamble_ac18_parity.rs` (name AND value, not just name), exactly
/// like `DeclarationKind`/`Visibility`/`EdgeReason` are checked above.
/// Deliberately plain `pub const` items (Messi Rule 17, anti-magic,
/// matching `reasons.rs`'s own rationale) rather than a bitflags type, and
/// declared at crate-root scope like everything else in this preamble so
/// evaluator code can reference them unqualified (e.g. `evidence &
/// RECEIVER_TYPE_MATCH != 0`).
pub(crate) const GRAPH_PREAMBLE_EXTRA_6: &str = r#"
pub const SAME_FILE: u16 = 1 << 0;
pub const SAME_PACKAGE: u16 = 1 << 1;
pub const IMPORTED: u16 = 1 << 2;
pub const STATIC_IMPORT: u16 = 1 << 3;
pub const WILDCARD_IMPORT: u16 = 1 << 4;
pub const ARITY_MATCH: u16 = 1 << 5;
pub const UNIQUE_NAME_IN_REPO: u16 = 1 << 6;
pub const QUALIFIED_NAME: u16 = 1 << 7;
pub const STRING_HEURISTIC: u16 = 1 << 8;
pub const INHERITANCE_FAMILY: u16 = 1 << 9;
pub const OVERLOAD_ARG_TYPE_MATCH: u16 = 1 << 10;
pub const FAMILY_TRUNCATED: u16 = 1 << 11;
pub const RECEIVER_TYPE_MATCH: u16 = 1 << 12;
pub const SAME_CLASS_OR_SUPER: u16 = 1 << 13;
pub const RECEIVER_TYPE_MISMATCH: u16 = 1 << 14;
"#;

/// Story #1787 AC8: dylib exports for a graph-mode evaluator --
/// `xray_collect_facts` + `xray_analyze_graph`, NEVER `xray_evaluate_node`
/// (ADR-001: "graph artifacts do not export xray_reduce_facts or
/// xray_drain_facts" and, symmetrically, never the legacy export either).
/// `xray_abi_version`/`xray_drain_debug_log` are duplicated from `EPILOGUE`
/// rather than factored into a shared tail constant -- both epilogues are
/// short, static text with no parameters to thread through, and a shared
/// helper would buy no real deduplication for two 6-line blocks while
/// adding a level of indirection to trace when reading either mode's
/// assembled source.
///
/// `xray_analyze_graph` wraps the call in `catch_unwind` INSIDE this same
/// compiled unit (the assembled evaluator source, panic and catch both
/// live in the SAME .so) and returns `Option<GraphResult>` -- `None` on a
/// caught panic, `Some` on success -- rather than letting a panic try to
/// unwind across the dylib boundary itself. `#[no_mangle] pub fn` (no
/// `extern "C"`) uses Rust's own calling convention, under which unwinding
/// across a dlopen'd .so is not a guarantee this codebase should depend
/// on; catching the panic before it ever crosses the boundary sidesteps
/// that question entirely -- only an already-safe plain value (`Option<
/// GraphResult>`) needs to cross, exactly like every other return value
/// here (`Vec<EvalFinding>`, `Vec<UserFact>`).
///
/// `xray_collect_facts` follows the IDENTICAL pattern (ADR-002 Defect 1
/// fix): it used to export `Vec<UserFact>` with no `catch_unwind` at all,
/// which meant a panic inside a user's `collect_facts` would unwind across
/// this dylib boundary uncaught -- empirically confirmed (via a disposable
/// scratch-copy repro) to abort the process with "Rust cannot catch
/// foreign exceptions", not merely a theoretical UB concern. It now
/// returns `Option<Vec<UserFact>>` -- `None` on a caught panic, `Some` on
/// success -- for exactly the same reason `xray_analyze_graph` already
/// does.
pub(crate) const GRAPH_EPILOGUE: &str = r#"
#[no_mangle]
pub fn xray_collect_facts(node: &OwnedNode, file: &str) -> Option<Vec<UserFact>> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| collect_facts(node, file))).ok()
}

#[no_mangle]
pub fn xray_analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Option<GraphResult> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| analyze_graph(g, facts))).ok()
}

/// Bug #1855 (H1): same rationale as EPILOGUE's identical trio -- see that
/// doc comment. Duplicated (not shared) because EPILOGUE and GRAPH_EPILOGUE
/// are independent string constants assembled into disjoint evaluator
/// sources.
#[no_mangle]
pub extern "C" fn xray_abi_version() -> u64 {
    XRAY_ABI_VERSION
}

/// Raw pointer half of the evaluator's embedded rustc version string,
/// read by the host loader BEFORE compatibility is established (H1).
#[no_mangle]
pub extern "C" fn xray_rustc_version_ptr() -> u64 {
    XRAY_RUSTC_VERSION.as_ptr() as u64
}

/// Length half of the evaluator's embedded rustc version string,
/// read by the host loader BEFORE compatibility is established (H1).
#[no_mangle]
pub extern "C" fn xray_rustc_version_len() -> u64 {
    XRAY_RUSTC_VERSION.len() as u64
}

#[no_mangle]
pub fn xray_drain_debug_log() -> Vec<String> {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        std::mem::take(&mut *log)
    })
}
"#;

/// Story #1792 (S3, AC1): the OPTIONAL `xray_refine` export -- "all-or-none
/// with the graph family" (ADR-001): only ever appended to the assembled
/// source when the user's code defines `fn refine`, checked via the same
/// `has_top_level_fn` AST-level detection `detect_evaluator_mode` already
/// uses (never a substring guess). Follows the IDENTICAL catch_unwind
/// pattern `xray_analyze_graph`/`xray_collect_facts` already establish: a
/// panic inside `refine` is caught INSIDE this compiled unit and reported
/// as `None`, never allowed to unwind across the dylib boundary (Rule 13).
pub(crate) const GRAPH_REFINE_EPILOGUE: &str = r#"
#[no_mangle]
pub fn xray_refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Option<Vec<EvalFinding>> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| refine(node, ctx, g, facts))).ok()
}
"#;
