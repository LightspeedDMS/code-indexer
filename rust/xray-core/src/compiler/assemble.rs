//! Issue #1934: evaluator source assembly and the single ABI-version
//! source of truth, extracted verbatim out of `compiler.rs` (pure move --
//! no behaviour change, see the module doc comment on `super`). Cache
//! identity (Bug #1784) is appended below in a second edit to keep each
//! individual change small; both concerns share this file because cache
//! identity is directly derived from the assembled source these functions
//! produce.

use super::preamble::{EPILOGUE, PREAMBLE};
use super::graph_preamble::{
    GRAPH_EPILOGUE, GRAPH_PREAMBLE_EXTRA_1, GRAPH_PREAMBLE_EXTRA_2, GRAPH_PREAMBLE_EXTRA_3,
    GRAPH_PREAMBLE_EXTRA_4, GRAPH_PREAMBLE_EXTRA_5, GRAPH_PREAMBLE_EXTRA_6, GRAPH_REFINE_EPILOGUE,
};
use super::types::{CompileError, CompileErrorKind};
use sha2::{Digest, Sha256};

/// ABI version of the compiled evaluator artifact. This is the SINGLE
/// source of truth (Bug #1784 review MAJOR-3, fixed): PREAMBLE below no
/// longer hardcodes a duplicate numeric literal -- it embeds a placeholder
/// token that `assemble_evaluator_source_with_preamble` substitutes with
/// THIS constant's value at assembly time, so the compiled evaluator's own
/// exported `xray_abi_version()` can never drift from it. `dynlib.rs`'s
/// loader also reads this constant directly (`crate::compiler::
/// XRAY_ABI_VERSION`) instead of declaring its own copy. `pub` so dynlib.rs
/// (same crate, different module) can reference it. Having a real value
/// here also lets the cache identity (Bug #1784) depend on the ABI version
/// as an explicit, independent component.
///
/// S2.5 (a prior slice of Story #1787) bumped this 2 -> 4, per ADR-001's
/// export table: ABI 4 is "the final two-mode contract" -- required
/// exports are `xray_abi_version` plus exactly one complete callback
/// family, either `xray_evaluate_node` (legacy) or `xray_collect_facts` +
/// `xray_analyze_graph` (graph mode); `xray_drain_debug_log`/`xray_refine`
/// remain optional. ABI 3 ("#1785 interim protocol": `xray_drain_facts` /
/// `xray_reduce_facts`) was DELIBERATELY skipped -- no such artifact was
/// ever compiled by this codebase, so there is no rolling-deployment
/// population to stay compatible with.
///
/// AC8 (this slice, per ADR-002) bumps this AGAIN, 4 -> 5: introducing the
/// `GraphHandle`/`FactsHandle` opaque accessor ABI changes what a graph-mode
/// artifact's exports actually mean -- `xray_analyze_graph` now receives
/// its graph through a handle-plus-accessor-functions dispatch table
/// instead of any prior shape -- so an ABI-4 graph artifact (compiled
/// before this accessor surface existed) must never be loaded as if it
/// matched. ADR-002 calls this out explicitly: "Introducing the handle and
/// accessors changes the ABI contract again and requires its own bump,
/// which the existing single-source-of-truth mechanism and the #1784
/// assembled-source cache identity handle automatically." Legacy
/// (`xray_evaluate_node`) artifacts are unaffected in shape, but still get
/// a fresh ABI/cache identity like every prior bump, since the ABI version
/// is a whole-artifact sentinel, not a per-mode one.
///
/// This slice (ADR-002 review follow-up) bumped this 5 -> 6: fixing two UB
/// defects found in the ABI-5 graph-mode surface changed its shape again.
/// (1) `xray_collect_facts` previously exported `Vec<UserFact>` with no
/// `catch_unwind` -- a panic inside a user's `collect_facts` unwound
/// across the dylib boundary uncaught, empirically confirmed (via a
/// disposable scratch-copy repro) to abort the process. It now exports
/// `Option<Vec<UserFact>>`, wrapped in `catch_unwind` exactly like
/// `xray_analyze_graph` already was. (2) `GraphHandle::resolve_symbol`/
/// `resolve_string` were HOST callback thunks that panicked on an
/// out-of-range id -- since these are called FROM INSIDE the dylib via a
/// stored function pointer, a panic there must unwind from host code back
/// into the calling dylib frame, which is a SECOND dylib-boundary crossing
/// that happens BEFORE the dylib's own `catch_unwind` around
/// `analyze_graph()` could ever intercept it (also empirically confirmed:
/// SIGABRT, "Rust cannot catch foreign exceptions"). Both accessors now
/// return `Option` instead of panicking. An ABI-5 graph artifact (compiled
/// before either fix) must never be loaded as if it matched ABI 6.
///
/// This slice (dual-review defect D2 fix) bumps this AGAIN, 6 -> 7:
/// `GraphHandle` gains two new accessor fn-pointer fields,
/// `is_symbol_referenced`/`is_definitely_dead_code`, exposing the AC6/D1
/// referenced-bit and completeness-aware dead-code verdict to
/// `analyze_graph` evaluators. Before this, `GraphHandle` exposed only
/// `callees_of`/`callers_of` (which read the POST-CAP candidate arena) --
/// an evaluator's only way to ask "is this referenced?" was
/// `callers_of(sym).is_empty()`, which reports a FALSE dead-code verdict
/// for a symbol whose only edge was capped away by the AC6 budget ladder,
/// even though `CodeGraph::is_definitely_dead_code` already reported it
/// correctly -- the guarantee AC6/D1 established was unreachable from the
/// one surface (`GraphHandle`) that produces findings. Adding these fields
/// changes `GraphHandle`'s memory layout, so an ABI-6 graph artifact
/// (compiled before this fix) must never be loaded as if it matched ABI 7.
///
/// Story #1792 (S3) bumps this AGAIN, 7 -> 8: `GraphHandle` gains a ninth
/// accessor field, `signature_for_raw_fn` (AC4 -- exposes the per-symbol
/// cached signature line for cross-file captioning without re-parsing),
/// changing `GraphHandle`'s memory layout again. The optional `xray_refine`
/// export (AC1) and the new `FileContext` mirror type land in this same
/// slice, so this is the one ABI bump covering all of S3's structural
/// changes together. An ABI-7 graph artifact (compiled before this fix)
/// must never be loaded as if it matched ABI 8.
///
/// Story #1785 bumps this AGAIN, 8 -> 9: wires up the previously-orphaned
/// `FactKey::Custom(InternedStr)` read path -- `FactsHandle` gains a second
/// accessor field, `for_custom_fn`, and its mirrored `UserFact` gains a
/// `custom_key: Option<String>` field so a `FactCollector` can name a
/// genuinely non-symbol fact (config key, event topic, structural hash)
/// instead of it always being silently attributed to whichever symbol
/// happens to enclose its line (ADR-001). Both changes alter memory layout
/// (`FactsHandle` gains a field; `UserFact` gains a field), so an ABI-8
/// graph artifact (compiled before this fix, whose `collect_facts`/
/// `analyze_graph` exports still use the 3-field `UserFact`/2-field
/// `FactsHandle` shape) must never be loaded as if it matched ABI 9 -- per
/// Bug #1784, the ABI version participates in the compile-cache identity,
/// so this bump is also what forces a correct cache-identity miss instead
/// of silently reusing a `.so` compiled against the stale layout.
///
/// Bug #1828 bumps this AGAIN, 9 -> 10: `GraphHandle` gains exact symbol
/// enumeration and SymbolId-to-dense-id lookup, making the advertised
/// dead-code and reachability evaluators expressible without a guessed bound.
/// An ABI-9 graph artifact must never be loaded as if it matched ABI 10.
///
/// Bug #1900 bumps this AGAIN, 10 -> 11: `GraphHandle` gains four new
/// accessor fn-pointer fields -- `location_for_raw_fn`, `declaration_kind_fn`,
/// `visibility_of_fn`, `edge_reason_fn` -- exposing per-symbol declaration
/// location/kind/visibility and a candidate-count-based edge tier, closing
/// the gap where every graph-mode finding shipped as an unchaseable bare
/// `name(N params)` string with no per-hop confidence at all. An ABI-10
/// graph artifact must never be loaded as if it matched ABI 11.
///
/// Bug #1900 review round 2 bumps this AGAIN, 11 -> 12: `GraphHandle` gains
/// a fifth new accessor fn-pointer field, `edge_evidence_fn`, exposing the
/// REAL `graph::reasons::*` evidence bits behind an edge -- the review
/// proved the ABI-11 `edge_reason` tier alone lets a fabricated edge (a
/// single surviving candidate backed by nothing stronger than
/// `SAME_PACKAGE`/`ARITY_MATCH`) report the SAME top tier as a genuinely
/// verified one, since that tier is a candidate COUNT, never a truth claim.
/// `edge_evidence` is what lets an evaluator require real evidence (e.g.
/// `RECEIVER_TYPE_MATCH`/`UNIQUE_NAME_IN_REPO`) before trusting a hop. An
/// ABI-11 graph artifact must never be loaded as if it matched ABI 12.
///
/// Bug #1901 bumps this AGAIN, 12 -> 13: `GraphHandle` gains a sixth new
/// accessor fn-pointer field, `reachable_to_fn` -- the CALLERS-direction
/// counterpart of `reachable_from_fn`. Before this, "how much of the
/// codebase can a change to a symbol affect" (the transitive CALLERS
/// closure) had no bounded primitive at all; an evaluator had to hand-roll
/// its own BFS over repeated `callers_of` calls, or -- the actually
/// observed production failure mode -- point the documented blast-radius
/// recipe at `reachable_from` instead, which follows the OPPOSITE
/// direction (what the root depends on) and silently returns just the root
/// for any class-level symbol (Type/Package/Field nodes carry no outbound
/// edges at all). An ABI-12 graph artifact must never be loaded as if it
/// matched ABI 13.
///
/// #1924/#1925 (epic #1906) bump this AGAIN, 13 -> 14: `GraphHandle` gains
/// five new accessor fn-pointer fields -- `callees_of_filtered_fn`,
/// `callers_of_filtered_fn`, `reachable_from_filtered_fn`, `reachable_to_
/// filtered_fn`, `strongly_connected_components_filtered_fn` -- the
/// EVIDENCE-FILTERED counterparts of the existing unfiltered traversal
/// primitives, gated by `(required_bits, forbidden_bits)` against `graph::
/// reasons::*`. Also a new `graph::reasons::RECEIVER_TYPE_MISMATCH` bit
/// (mirrored into `GRAPH_PREAMBLE_EXTRA_6` below) -- see `graph::reasons::
/// RECEIVER_TYPE_MISMATCH`'s own doc comment for the exact conditions
/// (the single source of truth for them) and `receiver_mismatch::apply_
/// receiver_type_mismatch_tagging` for the implementation, which TAGS
/// ONLY and never deletes from the raw graph. An ABI-13 graph artifact
/// must never be loaded as if it matched ABI 14.
pub const XRAY_ABI_VERSION: u64 = 14;

/// Placeholder token embedded in PREAMBLE in place of a hardcoded ABI
/// version literal. Substituted with the real `XRAY_ABI_VERSION` value by
/// `assemble_evaluator_source_with_preamble` before every compile -- this is
/// what makes `XRAY_ABI_VERSION` the ONE source of truth instead of a value
/// duplicated as text inside PREAMBLE (Bug #1784 review MAJOR-3).
pub(crate) const ABI_VERSION_PLACEHOLDER: &str = "__XRAY_ABI_VERSION_PLACEHOLDER__";
const RUSTC_VERSION_PLACEHOLDER: &str = "__XRAY_RUSTC_VERSION_PLACEHOLDER__";

/// Assemble a complete compilable .rs source from user evaluator code.
pub fn assemble_evaluator_source(user_code: &str) -> String {
    assemble_evaluator_source_with_preamble(PREAMBLE, user_code)
}

/// Assemble a complete compilable .rs source using a caller-supplied
/// preamble instead of the hardcoded PREAMBLE constant.
///
/// Substitutes ABI_VERSION_PLACEHOLDER in `preamble` with the real
/// XRAY_ABI_VERSION value (Bug #1784 review MAJOR-3: this is the ONE place
/// that resolves the placeholder, so the standalone constant can never
/// drift from what actually gets compiled into the evaluator).
///
/// The `preamble` parameter exists so a test can prove -- through the REAL
/// compile_evaluator pipeline, not just the standalone compute_cache_identity
/// hash function -- that changing ONLY the preamble text forces a fresh
/// compile (see compile_evaluator_with_preamble, #[cfg(test)] only).
/// Production code always goes through the public assemble_evaluator_source
/// above, which always passes the real PREAMBLE.
pub(crate) fn assemble_evaluator_source_with_preamble(preamble: &str, user_code: &str) -> String {
    assemble_evaluator_source_with_preamble_and_bounds(preamble, user_code).0
}

/// Bug #1929 rework (Codex P2): the bounds-returning counterpart of
/// `assemble_evaluator_source_with_preamble`, used by the compile
/// pipeline (`pipeline.rs::run_compile`) to remap rustc diagnostics.
/// Returns the SAME assembled string plus its exact `(user_code_start_
/// exclusive, user_code_end_inclusive)` line bounds -- see
/// `assemble_with_epilogue`'s own doc comment for why these are
/// computed structurally, never by re-parsing marker text.
pub(crate) fn assemble_evaluator_source_with_preamble_and_bounds(
    preamble: &str,
    user_code: &str,
) -> (String, (usize, usize)) {
    assemble_with_epilogue(preamble, user_code, EPILOGUE)
}

/// Shared assembly primitive both `assemble_evaluator_source_with_preamble`
/// (legacy) and `assemble_graph_evaluator_source` (AC8) build on: resolves
/// `ABI_VERSION_PLACEHOLDER` in `preamble`, then wraps `user_code` between
/// `preamble` and the caller-selected `epilogue`.
///
/// Bug #1929 rework (Codex P2): ALSO returns the exact `(user_code_
/// start_exclusive, user_code_end_inclusive)` line bounds, computed
/// DIRECTLY from the KNOWN line counts of the pieces this function
/// concatenates -- rather than searching the ASSEMBLED string for
/// marker text after the fact. The prior marker-search approach
/// (`diagnostic_line_bounds`, now removed) was provably unsound: user
/// code containing a line that happens to equal the literal marker text
/// (e.g. copy-pasted from this tool's own documentation) could truncate
/// or spoof the computed span, misclassifying a REAL user-code error as
/// generated "evaluator support code" (Codex-proven: a marker-lookalike
/// line at the user's own line 2 collapsed the span to `end=102` with a
/// genuine error at raw line 104, silently mislabeled). Computing bounds
/// from the KNOWN input pieces makes this class of injection
/// structurally impossible: nothing about `user_code`'s CONTENT can
/// ever change how many lines it itself occupies, so there is also NO
/// possible "bounds unavailable" case left to fall back from -- unlike
/// the removed marker-search version, this computation cannot fail.
///
/// `user_code_start_exclusive` is measured by constructing the EXACT
/// prefix (`"{resolved_preamble}\n// ---- USER CODE ----\n"`) this same
/// function concatenates ahead of `user_code` and counting ITS lines --
/// never `resolved_preamble.lines().count() + 1`, which silently
/// under-counts by one whenever `resolved_preamble` itself ends with a
/// trailing newline (true for both `PREAMBLE` and the graph preamble,
/// the exact off-by-one Bug #1929 item 4 already root-caused and fixed
/// once this session: `.lines()` does not report a text's own trailing
/// newline as a distinct entry, but the explicit `\n` separator here
/// DOES produce one real extra blank line once the marker follows).
fn assemble_with_epilogue(preamble: &str, user_code: &str, epilogue: &str) -> (String, (usize, usize)) {
    let rustc_version = crate::cache::get_rustc_version();
    let escaped_rustc_version = rustc_version.escape_default().to_string();
    let resolved_preamble = preamble
        .replace(ABI_VERSION_PLACEHOLDER, &XRAY_ABI_VERSION.to_string())
        .replace(RUSTC_VERSION_PLACEHOLDER, &escaped_rustc_version);
    let assembled = format!("{}\n// ---- USER CODE ----\n{}\n// ---- END USER CODE ----\n{}", resolved_preamble, user_code, epilogue);
    let user_code_start_exclusive = format!("{resolved_preamble}\n// ---- USER CODE ----\n").lines().count();
    let user_code_end_inclusive = user_code_start_exclusive + user_code.lines().count();
    (assembled, (user_code_start_exclusive, user_code_end_inclusive))
}

/// Story #1787 AC8 / Story #1792 (S3, AC1): assembles a graph-mode
/// evaluator's complete compilable source -- the COMMON `PREAMBLE`
/// (OwnedNode/EvalFinding/debug_log, shared with legacy mode) plus all 6
/// `GRAPH_PREAMBLE_EXTRA_*` slices (GraphHandle/FactsHandle/UserFact/
/// GraphResult/ReduceFinding/FileContext/reasons-bit-constants), followed by
/// user code, followed by `GRAPH_EPILOGUE` (xray_collect_facts +
/// xray_analyze_graph, never xray_evaluate_node) plus `GRAPH_REFINE_EPILOGUE`
/// (xray_refine) ONLY when `user_code` defines `fn refine` -- AC1's
/// "all-or-none with the graph family" applies to whether the export exists
/// at all, not to whether this function is invoked.
pub(crate) fn assemble_graph_evaluator_source(user_code: &str) -> String {
    assemble_graph_evaluator_source_and_bounds(user_code).0
}

/// Bug #1929 rework (Codex P2): the bounds-returning counterpart of
/// `assemble_graph_evaluator_source`, used by the compile pipeline
/// (`pipeline.rs::run_compile`) to remap rustc diagnostics in graph
/// mode. See `assemble_with_epilogue`'s own doc comment for why the
/// bounds are computed structurally, never by re-parsing marker text.
pub(crate) fn assemble_graph_evaluator_source_and_bounds(user_code: &str) -> (String, (usize, usize)) {
    let epilogue = if has_top_level_fn(user_code, "refine") {
        format!("{}\n{}", GRAPH_EPILOGUE, GRAPH_REFINE_EPILOGUE)
    } else {
        GRAPH_EPILOGUE.to_string()
    };
    assemble_with_epilogue(&graph_preamble_text(), user_code, &epilogue)
}

/// The full graph-mode preamble text (the COMMON `PREAMBLE`, shared with
/// legacy mode, plus all 6 `GRAPH_PREAMBLE_EXTRA_*` slices) -- factored
/// out of `assemble_graph_evaluator_source` so `preamble_line_count`
/// (Bug #1929 item 4) measures the IDENTICAL text that function actually
/// assembles ahead of user code, rather than a second, independently
/// maintained copy of this concatenation that could silently drift from
/// it (Rule 4, anti-duplication).
fn graph_preamble_text() -> String {
    format!(
        "{}\n{}\n{}\n{}\n{}\n{}\n{}",
        PREAMBLE,
        GRAPH_PREAMBLE_EXTRA_1,
        GRAPH_PREAMBLE_EXTRA_2,
        GRAPH_PREAMBLE_EXTRA_3,
        GRAPH_PREAMBLE_EXTRA_4,
        GRAPH_PREAMBLE_EXTRA_5,
        GRAPH_PREAMBLE_EXTRA_6,
    )
}


/// Returns true only if `source` contains an actual top-level `fn` named
/// `name` -- never just the text appearing in a comment or string
/// literal. Shared by `has_evaluate_node_fn` and `detect_evaluator_mode`.
pub(crate) fn has_top_level_fn(source: &str, name: &str) -> bool {
    let file: syn::File = match syn::parse_str(source) {
        Ok(f) => f,
        Err(_) => return false,
    };
    file.items.iter().any(|item| matches!(item, syn::Item::Fn(func) if func.sig.ident == name))
}

/// AC8 / ADR-001: "After S2, X-Ray supports exactly two evaluator
/// execution modes" -- `Legacy` (`evaluate_node`) and `Graph`
/// (`collect_facts` + `analyze_graph`). No third variant: `Ambiguous`/
/// `mixed` is a `CompileError`, never a mode value, exactly like AC4's
/// `Confidence` deliberately has no `Ambiguous` variant for the same
/// reason -- classification failures are errors, not states.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EvaluatorMode {
    Legacy,
    Graph,
}

/// AC8: "A scan provides evaluate_node OR graph mode — validated
/// synchronously before job submission, not discovered at runtime."
/// ADR-001: "A graph evaluator must not export evaluate_node; a legacy
/// evaluator must not be treated as graph mode. The loader rejects a
/// mixed or incomplete callback family." This is the SYNCHRONOUS,
/// AST-level check that classification: never a fallback guess, never
/// silently defaulting to one mode when the source is ambiguous.
pub fn detect_evaluator_mode(source: &str) -> Result<EvaluatorMode, CompileError> {
    let has_legacy = has_top_level_fn(source, "evaluate_node");
    let has_collect_facts = has_top_level_fn(source, "collect_facts");
    let has_analyze_graph = has_top_level_fn(source, "analyze_graph");
    let has_any_graph_fn = has_collect_facts || has_analyze_graph;

    match (has_legacy, has_any_graph_fn, has_collect_facts, has_analyze_graph) {
        (true, false, _, _) => Ok(EvaluatorMode::Legacy),
        (false, true, true, true) => Ok(EvaluatorMode::Graph),
        (true, true, _, _) => Err(CompileError {
            message: "Evaluator defines both legacy evaluate_node and graph-mode callbacks \
                      (collect_facts/analyze_graph) -- exactly one mode family is allowed"
                .to_string(),
            details: vec![],
            kind: CompileErrorKind::Compile,
        }),
        (false, true, _, _) => Err(CompileError {
            message: "Graph mode requires BOTH collect_facts and analyze_graph -- one is missing".to_string(),
            details: vec![],
            kind: CompileErrorKind::Compile,
        }),
        (false, false, _, _) => Err(CompileError {
            message: "Evaluator must define either fn evaluate_node(...) (legacy mode) or both \
                      fn collect_facts(...) and fn analyze_graph(...) (graph mode)"
                .to_string(),
            details: vec![],
            kind: CompileErrorKind::Compile,
        }),
    }
}

pub(crate) fn sha256_hex(input: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(input.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Bug #1784: the ONE shared cache identity for a compiled evaluator
/// artifact. Combines the assembled source (user code wrapped in PREAMBLE +
/// EPILOGUE), the ABI version, and the rustc toolchain version into a single
/// SHA-256 hex digest.
///
/// ANY change to user code, PREAMBLE, EPILOGUE, XRAY_ABI_VERSION, or the
/// rustc toolchain therefore produces a DIFFERENT identity — a stale
/// artifact compiled under old inputs can never be mistaken for a hit
/// against new inputs. This is used as:
///   - the local `.so`/`.meta` filename stem (see compile_evaluator),
///   - the value written into PostgreSQL's existing `source_hash` TEXT
///     primary key column (no schema change — see xray_cache_backend.py).
///
/// A `\u{0}` (NUL) separator is used between components — assembled_source
/// can contain arbitrary text (including digits and colons), so a
/// human-readable separator like ":" could theoretically produce a field
/// boundary collision; NUL never appears in valid Rust source text.
pub fn compute_cache_identity(assembled_source: &str, abi_version: u64, rustc_version: &str) -> String {
    let combined = format!("{}\u{0}{}\u{0}{}", assembled_source, abi_version, rustc_version);
    sha256_hex(&combined)
}

/// Bundle of the composite identity plus its individual input components.
///
/// `source_hash` (hash of the assembled source alone) and `abi_version` are
/// exposed separately from `identity` so the LOCAL `.meta` file can record
/// them as individually-checkable, debuggable fields (Bug #1784 requirement
/// #4), in addition to `identity` being used as the opaque filename/PG key.
#[derive(Debug, Clone, PartialEq)]
pub struct CacheIdentityInfo {
    pub identity: String,
    pub source_hash: String,
    pub abi_version: u64,
    pub rustc_version: String,
}

/// Compute identity info from an already-assembled source string (avoids
/// re-assembling when the caller already has it, e.g. compile_evaluator).
pub fn cache_identity_info_from_source(assembled_source: &str) -> CacheIdentityInfo {
    // Deliberately identify the evaluator artifact with the pinned compiler
    // that builds it. Host compatibility is checked separately by the loader
    // against the build-time host version exported by build.rs; mixing that
    // host value into this artifact identity would not replace the required
    // hard loader check.
    let rustc_version = crate::cache::get_rustc_version();
    let source_hash = sha256_hex(assembled_source);
    let identity = compute_cache_identity(assembled_source, XRAY_ABI_VERSION, &rustc_version);
    CacheIdentityInfo {
        identity,
        source_hash,
        abi_version: XRAY_ABI_VERSION,
        rustc_version,
    }
}

/// Compute identity info directly from raw user code (assembles internally).
/// This is the entry point used by `xray-cli --print-cache-identity`, which
/// starts from raw user code read off stdin and has no pre-assembled source.
///
/// LEGACY-MODE ONLY (H9, consolidated review, Issue #1811/Bug #1812): this
/// always assembles via `assemble_evaluator_source` regardless of the
/// evaluator's real mode. For a graph-mode evaluator, use
/// `cache_identity_info_graph` instead -- `compile_evaluator_impl` itself
/// assembles graph-mode sources via `assemble_graph_evaluator_source`, and
/// the two assemblies produce DIFFERENT identities (different preamble/
/// epilogue text hashed into the composite). Calling this function on
/// graph-mode source computes an identity that will never match the real
/// compiled `.so` filename.
pub fn cache_identity_info(user_code: &str) -> CacheIdentityInfo {
    cache_identity_info_from_source(&assemble_evaluator_source(user_code))
}

/// H9 (consolidated review, Issue #1811/Bug #1812): the graph-mode
/// counterpart of `cache_identity_info` -- assembles via
/// `assemble_graph_evaluator_source`, EXACTLY mirroring the assembly
/// `compile_evaluator_impl` performs for `EvaluatorMode::Graph` (Step 3),
/// so the identity this returns always matches the real `.so` filename a
/// graph-mode compile produces. This is the entry point
/// `xray-cli --print-cache-identity --graph-mode` uses.
pub fn cache_identity_info_graph(user_code: &str) -> CacheIdentityInfo {
    cache_identity_info_from_source(&assemble_graph_evaluator_source(user_code))
}
