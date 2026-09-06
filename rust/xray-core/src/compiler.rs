/// Evaluator compilation pipeline: validate → assemble → compile → cache.
use crate::cache::{self, CacheMetadata};
use crate::validator;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::time::Instant;

const MAX_CACHE_ENTRIES: usize = 100;

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
/// This slice (ADR-002 review follow-up) bumps this AGAIN, 5 -> 6: fixing
/// two UB defects found in the ABI-5 graph-mode surface changes its shape
/// again. (1) `xray_collect_facts` previously exported `Vec<UserFact>` with
/// no `catch_unwind` -- a panic inside a user's `collect_facts` unwound
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
pub const XRAY_ABI_VERSION: u64 = 6;

/// Placeholder token embedded in PREAMBLE in place of a hardcoded ABI
/// version literal. Substituted with the real `XRAY_ABI_VERSION` value by
/// `assemble_evaluator_source_with_preamble` before every compile -- this is
/// what makes `XRAY_ABI_VERSION` the ONE source of truth instead of a value
/// duplicated as text inside PREAMBLE (Bug #1784 review MAJOR-3).
const ABI_VERSION_PLACEHOLDER: &str = "__XRAY_ABI_VERSION_PLACEHOLDER__";

/// Result of a successful compilation.
#[derive(Debug)]
pub struct CompileResult {
    pub so_path: PathBuf,
    pub compile_ms: u128,
    pub cached: bool,
}

/// Error from the compilation pipeline.
#[derive(Debug)]
pub struct CompileError {
    pub message: String,
    pub details: Vec<String>,
}

impl std::fmt::Display for CompileError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)?;
        for d in &self.details {
            write!(f, "\n  {}", d)?;
        }
        Ok(())
    }
}

/// The evaluator preamble defines OwnedNode and EvalFinding so user code
/// can reference them without imports. Layout MUST match xray_core types.
///
/// `truncate_snippet` is included as a utility helper for user evaluator code
/// that needs to trim long text snippets before storing them in EvalFinding.
///
/// `XRAY_ABI_VERSION` is exported so the loader can verify the compiled .so
/// was built with a compatible type layout before calling the evaluate function.
pub(crate) const PREAMBLE: &str = r#"
/// ABI version sentinel — substituted from the single source of truth
/// (compiler::XRAY_ABI_VERSION) by assemble_evaluator_source_with_preamble
/// before every compile (Bug #1784 review MAJOR-3).
const XRAY_ABI_VERSION: u64 = __XRAY_ABI_VERSION_PLACEHOLDER__;

use std::sync::Arc;

#[derive(Debug, Clone)]
pub struct OwnedNode {
    pub kind: String,
    pub start_line: usize,
    pub start_byte: usize,
    pub end_byte: usize,
    pub children: Vec<OwnedNode>,
    pub is_named: bool,
    source: Arc<str>,
}

impl OwnedNode {
    pub fn text(&self) -> &str {
        self.source.get(self.start_byte..self.end_byte).unwrap_or("")
    }
    pub fn named_children(&self) -> Vec<&OwnedNode> {
        self.children.iter().filter(|c| c.is_named).collect()
    }
    pub fn child_by_kind(&self, kind: &str) -> Option<&OwnedNode> {
        self.children.iter().find(|c| c.kind == kind)
    }
    // Bug #1795: explicit-stack (heap) traversal instead of recursion, so a
    // deeply nested OwnedNode passed into a compiled evaluator cannot
    // SIGABRT the process. Mirrors owned_node.rs's fix exactly (known
    // duplication debt, tracked separately as AC16 on #1787 -- not
    // refactored here).
    pub fn has_descendant_of_kind(&self, kind: &str) -> bool {
        let mut stack: Vec<&OwnedNode> = self.children.iter().collect();
        while let Some(node) = stack.pop() {
            if node.kind == kind { return true; }
            stack.extend(node.children.iter());
        }
        false
    }
    pub fn descendants_of_kind(&self, kind: &str) -> Vec<&OwnedNode> {
        let mut results = Vec::new();
        let mut stack: Vec<&OwnedNode> = self.children.iter().rev().collect();
        while let Some(node) = stack.pop() {
            if node.kind == kind { results.push(node); }
            stack.extend(node.children.iter().rev());
        }
        results
    }
}

#[derive(Debug, Clone)]
pub struct EvalFinding {
    pub pattern: String,
    pub line: usize,
    pub snippet: String,
}

/// Utility for user evaluator code: collapse whitespace and truncate to max_len bytes.
/// Truncation always falls on a UTF-8 char boundary — never panics on multibyte chars.
/// If truncation occurs, appends "...".
fn truncate_snippet(s: &str, max_len: usize) -> String {
    let collapsed: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.len() <= max_len {
        collapsed
    } else {
        let boundary = collapsed
            .char_indices()
            .map(|(i, _)| i)
            .take_while(|&i| i <= max_len)
            .last()
            .unwrap_or(0);
        format!("{}...", &collapsed[..boundary])
    }
}

use std::cell::RefCell;

thread_local! {
    static DEBUG_LOG: RefCell<Vec<String>> = RefCell::new(Vec::new());
}

/// Debug logging helper for evaluator development.
/// Messages are collected in a per-thread buffer (max 100 messages, 10KB total)
/// and returned alongside findings in the JSON output as debug_messages[].
/// Calls past the limits are silently dropped.
fn debug_log(msg: &str) {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        if log.len() < 100 {
            let total_bytes: usize = log.iter().map(|s| s.len()).sum();
            if total_bytes + msg.len() <= 10240 {
                log.push(msg.to_string());
            }
        }
    });
}
"#;

const EPILOGUE: &str = r#"
#[no_mangle]
pub fn xray_evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    evaluate_node(node)
}

#[no_mangle]
pub fn xray_abi_version() -> u64 {
    XRAY_ABI_VERSION
}

#[no_mangle]
pub fn xray_drain_debug_log() -> Vec<String> {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        std::mem::take(&mut *log)
    })
}
"#;

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
    shortest_path_to_any_fn: fn(*const (), u32, &[u32], usize) -> Option<Vec<u32>>,
    strongly_connected_components_fn: fn(*const ()) -> Vec<Vec<u32>>,
    resolve_symbol_fn: fn(*const (), u32) -> Option<u64>,
    resolve_string_raw_fn: fn(*const (), u32) -> Option<(*const u8, usize)>,
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
"#;

/// Continues `GRAPH_PREAMBLE_EXTRA_1`/`_2` -- see the first's doc comment.
/// Closes `GraphHandle`'s impl block with its final accessor
/// (`resolve_string`), then mirrors the REAL `UserFact` and `FactsHandle`
/// types (`graph::user_facts`) the same way: `FactsHandle` carries only an
/// opaque context pointer plus one accessor function pointer, never
/// `FactIndex`'s internal `HashMap` layout (the same ADR-002 principle
/// extended from `CodeGraph` to `FactIndex`).
pub(crate) const GRAPH_PREAMBLE_EXTRA_3: &str = r#"
    pub fn resolve_string(&self, string_id: u32) -> Option<&str> {
        let (ptr, len) = (self.resolve_string_raw_fn)(self.ctx, string_id)?;
        Some(unsafe { std::str::from_utf8_unchecked(std::slice::from_raw_parts(ptr, len)) })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UserFact {
    pub kind: String,
    pub line: usize,
    pub message: String,
}

#[derive(Clone, Copy)]
pub struct FactsHandle<'facts> {
    ctx: *const (),
    for_symbol_fn: fn(*const (), u64) -> Vec<UserFact>,
    _facts: std::marker::PhantomData<&'facts ()>,
}

impl<'facts> FactsHandle<'facts> {
    pub fn for_symbol(&self, symbol: SymbolId) -> Vec<UserFact> {
        (self.for_symbol_fn)(self.ctx, symbol)
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
const GRAPH_EPILOGUE: &str = r#"
#[no_mangle]
pub fn xray_collect_facts(node: &OwnedNode, file: &str) -> Option<Vec<UserFact>> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| collect_facts(node, file))).ok()
}

#[no_mangle]
pub fn xray_analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Option<GraphResult> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| analyze_graph(g, facts))).ok()
}

#[no_mangle]
pub fn xray_abi_version() -> u64 {
    XRAY_ABI_VERSION
}

#[no_mangle]
pub fn xray_drain_debug_log() -> Vec<String> {
    DEBUG_LOG.with(|log| {
        let mut log = log.borrow_mut();
        std::mem::take(&mut *log)
    })
}
"#;

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
fn assemble_evaluator_source_with_preamble(preamble: &str, user_code: &str) -> String {
    assemble_with_epilogue(preamble, user_code, EPILOGUE)
}

/// Shared assembly primitive both `assemble_evaluator_source_with_preamble`
/// (legacy) and `assemble_graph_evaluator_source` (AC8) build on: resolves
/// `ABI_VERSION_PLACEHOLDER` in `preamble`, then wraps `user_code` between
/// `preamble` and the caller-selected `epilogue`.
fn assemble_with_epilogue(preamble: &str, user_code: &str, epilogue: &str) -> String {
    let resolved_preamble = preamble.replace(ABI_VERSION_PLACEHOLDER, &XRAY_ABI_VERSION.to_string());
    format!("{}\n// ---- USER CODE ----\n{}\n// ---- END USER CODE ----\n{}", resolved_preamble, user_code, epilogue)
}

/// Story #1787 AC8: assembles a graph-mode evaluator's complete compilable
/// source -- the COMMON `PREAMBLE` (OwnedNode/EvalFinding/debug_log, shared
/// with legacy mode) plus all 4 `GRAPH_PREAMBLE_EXTRA_*` slices (GraphHandle/
/// FactsHandle/UserFact/GraphResult/ReduceFinding), followed by user code,
/// followed by `GRAPH_EPILOGUE` (xray_collect_facts + xray_analyze_graph,
/// never xray_evaluate_node).
fn assemble_graph_evaluator_source(user_code: &str) -> String {
    let preamble = format!(
        "{}\n{}\n{}\n{}\n{}",
        PREAMBLE, GRAPH_PREAMBLE_EXTRA_1, GRAPH_PREAMBLE_EXTRA_2, GRAPH_PREAMBLE_EXTRA_3, GRAPH_PREAMBLE_EXTRA_4
    );
    assemble_with_epilogue(&preamble, user_code, GRAPH_EPILOGUE)
}

/// Number of lines in the preamble (for adjusting rustc error line numbers).
pub fn preamble_line_count() -> usize {
    PREAMBLE.lines().count() + 1 // +1 for the "USER CODE" comment
}

/// Compile user evaluator code into a cached .so file.
///
/// Pipeline: validate → hash → cache check → assemble → compile → save
pub fn compile_evaluator(user_code: &str, cache_dir: &Path) -> Result<CompileResult, CompileError> {
    compile_evaluator_impl(user_code, cache_dir, PREAMBLE)
}

/// Test-only seam (Bug #1784 review MAJOR-1): compiles using a
/// caller-supplied preamble instead of the hardcoded PREAMBLE constant, so
/// a genuine integration test can prove -- through the REAL compile_evaluator
/// pipeline -- that changing ONLY the preamble text (user code, ABI version,
/// and rustc toolchain held fixed) forces a fresh compile (cached == false),
/// never a stale-artifact reuse. This is the test class that would catch a
/// future regression where the cache identity is again derived from raw
/// user_code alone while the composite-identity helper functions are left
/// in place unchanged. Exists ONLY under #[cfg(test)]; production code can
/// never call it.
#[cfg(test)]
pub(crate) fn compile_evaluator_with_preamble(
    user_code: &str,
    cache_dir: &Path,
    preamble: &str,
) -> Result<CompileResult, CompileError> {
    compile_evaluator_impl(user_code, cache_dir, preamble)
}

fn compile_evaluator_impl(user_code: &str, cache_dir: &Path, preamble: &str) -> Result<CompileResult, CompileError> {
    // Step 1: Validate
    if let Err(errors) = validator::validate_evaluator_source(user_code) {
        return Err(CompileError {
            message: "Evaluator validation failed".to_string(),
            details: errors.iter().map(|e| e.to_string()).collect(),
        });
    }

    // Step 2: AC8 -- classify the evaluator's mode SYNCHRONOUSLY, before
    // any compilation is attempted. `detect_evaluator_mode` subsumes the
    // old "must define evaluate_node" check: it IS that check for the
    // Legacy case, plus the symmetric Graph-mode check ADR-001 requires,
    // with mixed/incomplete callback families rejected as errors rather
    // than silently guessed at.
    let mode = detect_evaluator_mode(user_code)?;

    // Step 3: Compute the ONE shared cache identity (Bug #1784) — sensitive
    // to the full assembled source (PREAMBLE + user code + EPILOGUE), the
    // ABI version, and the rustc toolchain. Computed from the assembled
    // source ONCE here and reused below for the actual compile (Step 5b),
    // so PREAMBLE/EPILOGUE text is never re-derived redundantly. The
    // assembler itself is mode-correct: Legacy uses the caller-supplied
    // `preamble` + legacy EPILOGUE; Graph always uses the full
    // GRAPH_PREAMBLE_EXTRA_* mirror + GRAPH_EPILOGUE (AC8).
    let assembled_source = match mode {
        EvaluatorMode::Legacy => assemble_evaluator_source_with_preamble(preamble, user_code),
        EvaluatorMode::Graph => assemble_graph_evaluator_source(user_code),
    };
    let identity_info = cache_identity_info_from_source(&assembled_source);
    let so_path = cache_dir.join(format!("{}.so", identity_info.identity));
    let meta_path = cache_dir.join(format!("{}.meta", identity_info.identity));

    // Step 4: Cache check. The filename itself already encodes source_hash +
    // abi_version + rustc_version, so a mismatch on any of them means the
    // file simply won't be found. The per-field checks below are defense in
    // depth against a corrupted/hand-edited .meta sitting at a colliding
    // filename — a mismatch on ANY field is ALWAYS a MISS, never a fallback
    // match.
    if so_path.exists() {
        if let Some(meta) = cache::read_metadata(&meta_path) {
            if meta.rustc_version == identity_info.rustc_version
                && meta.abi_version == identity_info.abi_version
                && meta.source_hash == identity_info.source_hash
                && cache::is_fresh(&meta.compiled_at, cache::LOCAL_CACHE_TTL_SECS)
            {
                return Ok(CompileResult {
                    so_path,
                    compile_ms: 0,
                    cached: true,
                });
            }
        }
    }

    // Step 5: Create cache dir
    std::fs::create_dir_all(cache_dir).map_err(|e| CompileError {
        message: format!("Failed to create cache directory '{}': {}", cache_dir.display(), e),
        details: vec![],
    })?;

    // Step 5b: Bug #1425 — isolate this compile into a private, per-invocation
    // build directory instead of writing the .rs source / -o output directly
    // into the shared cache_dir. Two concurrent compiles of the SAME evaluator
    // hash use the identical crate name (derived from the source filename), so
    // rustc's LLVM codegen-unit intermediate object files (*.rcgu.o) — written
    // alongside the -o path — collide by filename when both processes target
    // cache_dir directly, producing "rust-lld: error: cannot open <hash>.rcgu.o:
    // No such file or directory" for whichever process loses the race.
    // tempdir_in(cache_dir) guarantees the build dir is on the SAME filesystem
    // as cache_dir, so publishing the finished .so via rename() below is a
    // single atomic syscall. The TempDir guard recursively removes the build
    // directory (source + any rustc scratch files) on every exit path —
    // success or the early '?' returns below — leaving nothing to leak.
    let identity = &identity_info.identity;
    let build_dir = tempfile::Builder::new()
        .prefix(&format!("build-{}-", &identity[..identity.len().min(16)]))
        .tempdir_in(cache_dir)
        .map_err(|e| CompileError {
            message: format!(
                "Failed to create isolated build directory in '{}': {}",
                cache_dir.display(),
                e
            ),
            details: vec![],
        })?;
    let build_rs_path = build_dir.path().join(format!("{}.rs", identity));
    let build_so_path = build_dir.path().join(format!("{}.so", identity));

    std::fs::write(&build_rs_path, &assembled_source).map_err(|e| CompileError {
        message: format!("Failed to write evaluator source: {}", e),
        details: vec![],
    })?;

    // Step 6: Compile — output goes into the isolated build dir, never
    // directly into the shared cache_dir, so no two concurrent invocations
    // ever share a -o directory.
    let compile_start = Instant::now();
    let output = std::process::Command::new("rustc")
        .args([
            "--edition", "2021",
            "--crate-type", "cdylib",
            "-C", "opt-level=2",
            "-o", build_so_path.to_str().unwrap(),
            build_rs_path.to_str().unwrap(),
        ])
        .output()
        .map_err(|e| CompileError {
            message: format!("Failed to invoke rustc: {}", e),
            details: vec!["Is rustc installed and on PATH?".to_string()],
        })?;
    let compile_ms = compile_start.elapsed().as_millis();

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        let preamble_lines = preamble_line_count();
        let adjusted = adjust_error_lines(&stderr, preamble_lines);
        return Err(CompileError {
            message: "Evaluator compilation failed".to_string(),
            details: adjusted,
        });
    }

    // Step 6b: Atomically publish the compiled .so into the shared cache_dir.
    // A concurrent compile of the SAME hash may publish first — that's fine,
    // since both compiles started from byte-identical source; rename() simply
    // replaces so_path with an equally-valid artifact.
    std::fs::rename(&build_so_path, &so_path).map_err(|e| CompileError {
        message: format!(
            "Failed to publish compiled evaluator to '{}': {}",
            so_path.display(),
            e
        ),
        details: vec![],
    })?;

    // Step 7: Write metadata (best-effort — .so already exists, warn but don't fail)
    let now = chrono_now_iso();
    if let Err(e) = cache::write_metadata(&meta_path, &CacheMetadata {
        source_hash: identity_info.source_hash.clone(),
        rustc_version: identity_info.rustc_version.clone(),
        abi_version: identity_info.abi_version,
        compiled_at: now,
        compile_ms,
    }) {
        eprintln!("xray: warning: failed to write cache metadata {}: {}", meta_path.display(), e);
    }

    // Step 8: LRU eviction
    cache::evict_lru(cache_dir, MAX_CACHE_ENTRIES);

    Ok(CompileResult {
        so_path,
        compile_ms,
        cached: false,
    })
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

/// Returns true only if `source` contains an actual top-level `fn` named
/// `name` -- never just the text appearing in a comment or string
/// literal. Shared by `has_evaluate_node_fn` and `detect_evaluator_mode`.
fn has_top_level_fn(source: &str, name: &str) -> bool {
    let file: syn::File = match syn::parse_str(source) {
        Ok(f) => f,
        Err(_) => return false,
    };
    file.items.iter().any(|item| matches!(item, syn::Item::Fn(func) if func.sig.ident == name))
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
        }),
        (false, true, _, _) => Err(CompileError {
            message: "Graph mode requires BOTH collect_facts and analyze_graph -- one is missing".to_string(),
            details: vec![],
        }),
        (false, false, _, _) => Err(CompileError {
            message: "Evaluator must define either fn evaluate_node(...) (legacy mode) or both \
                      fn collect_facts(...) and fn analyze_graph(...) (graph mode)"
                .to_string(),
            details: vec![],
        }),
    }
}

fn sha256_hex(input: &str) -> String {
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
    let rustc_version = cache::get_rustc_version();
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
pub fn cache_identity_info(user_code: &str) -> CacheIdentityInfo {
    cache_identity_info_from_source(&assemble_evaluator_source(user_code))
}

/// Adjust rustc error line numbers by subtracting the preamble offset.
pub fn adjust_error_lines(stderr: &str, preamble_lines: usize) -> Vec<String> {
    let mut result = Vec::new();
    for line in stderr.lines() {
        // rustc errors look like: "  --> filename.rs:LINE:COL"
        if let Some(arrow_pos) = line.find("--> ") {
            let after = &line[arrow_pos + 4..];
            if let Some(colon1) = after.find(':') {
                let after_colon1 = &after[colon1 + 1..];
                if let Some(colon2) = after_colon1.find(':') {
                    let line_str = &after_colon1[..colon2];
                    if let Ok(orig_line) = line_str.parse::<usize>() {
                        let adjusted = orig_line.saturating_sub(preamble_lines);
                        let new_line = line.replacen(
                            &format!(":{}", orig_line),
                            &format!(":{}", adjusted),
                            1,
                        );
                        result.push(new_line);
                        continue;
                    }
                }
            }
        }
        result.push(line.to_string());
    }
    result
}

/// Simple timestamp without external dependency.
fn chrono_now_iso() -> String {
    let duration = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    format!("{}s-since-epoch", duration.as_secs())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    /// RED phase: `assemble_graph_evaluator_source` does not exist yet.
    /// This proves what its GREEN implementation must do -- assemble
    /// graph-mode source using the GRAPH_PREAMBLE_EXTRA_* mirror text and
    /// GRAPH_EPILOGUE, NEVER the legacy `xray_evaluate_node` export.
    #[test]
    fn assemble_graph_evaluator_source_uses_graph_preamble_and_epilogue_not_legacy() {
        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
        let assembled = assemble_graph_evaluator_source(user_code);
        assert!(assembled.contains("pub struct GraphHandle"), "must contain GraphHandle mirror");
        assert!(assembled.contains("pub struct FactsHandle"), "must contain FactsHandle mirror");
        assert!(assembled.contains("xray_collect_facts"), "must export xray_collect_facts");
        assert!(assembled.contains("xray_analyze_graph"), "must export xray_analyze_graph");
        assert!(assembled.contains(user_code), "must contain user code verbatim");
        assert!(!assembled.contains("xray_evaluate_node"), "graph mode must NEVER export xray_evaluate_node");
    }

    #[test]
    fn test_assemble_contains_preamble_and_epilogue() {
        let user_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }";
        let assembled = assemble_evaluator_source(user_code);
        assert!(assembled.contains("pub struct OwnedNode"), "should contain preamble OwnedNode");
        assert!(assembled.contains("pub struct EvalFinding"), "should contain preamble EvalFinding");
        assert!(assembled.contains("xray_evaluate_node"), "should contain epilogue symbol");
        assert!(assembled.contains("evaluate_node(node)"), "should call evaluate_node in epilogue");
        assert!(assembled.contains(user_code), "should contain user code verbatim");
    }

    #[test]
    fn test_sha256_consistent() {
        let a = sha256_hex("hello world");
        let b = sha256_hex("hello world");
        assert_eq!(a, b, "sha256 must be deterministic");
        assert_eq!(a.len(), 64, "SHA-256 hex must be 64 chars");
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()), "must be hex");

        let c = sha256_hex("different input");
        assert_ne!(a, c, "different inputs must produce different hashes");
    }

    /// AC8: `compile_evaluator_impl`'s mode-aware branching must accept a
    /// genuine graph-mode evaluator (one `detect_evaluator_mode` classifies
    /// as `EvaluatorMode::Graph`), assemble it with `GRAPH_PREAMBLE_EXTRA_*`
    /// and `GRAPH_EPILOGUE`, compile it to a real `.so`, and that `.so`
    /// must actually EXPORT the graph-mode symbols -- never `xray_evaluate_node`.
    #[test]
    fn compile_evaluator_compiles_a_valid_graph_mode_evaluator() {
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    for symbol in g.reachable_from(&[0], 5) {
        result.refine.push(g.resolve_symbol(symbol).expect("symbol came from g.reachable_from, always valid"));
    }
    result
}
"#;
        assert_eq!(
            detect_evaluator_mode(user_code).unwrap(),
            EvaluatorMode::Graph,
            "test fixture assumption broken: this source must classify as Graph mode"
        );

        let result = compile_evaluator(user_code, dir.path())
            .expect("a genuine graph-mode evaluator must compile successfully");
        assert!(result.so_path.exists(), ".so file must exist on disk");

        let lib = unsafe { libloading::Library::new(&result.so_path) }
            .expect("compiled graph-mode .so must load successfully");
        unsafe {
            let has_collect_facts: bool = lib.get::<extern "Rust" fn()>(b"xray_collect_facts\0").is_ok();
            let has_analyze_graph: bool = lib.get::<extern "Rust" fn()>(b"xray_analyze_graph\0").is_ok();
            let has_legacy: bool = lib.get::<extern "Rust" fn()>(b"xray_evaluate_node\0").is_ok();
            assert!(has_collect_facts, "graph-mode .so must export xray_collect_facts");
            assert!(has_analyze_graph, "graph-mode .so must export xray_analyze_graph");
            assert!(!has_legacy, "graph-mode .so must NEVER export xray_evaluate_node");
        }
    }

    #[test]
    fn test_compile_valid_evaluator() {
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    if node.kind == "try_statement" {
        findings.push(EvalFinding {
            pattern: "test".to_string(),
            line: node.start_line,
            snippet: String::new(),
        });
    }
    findings
}
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_ok(), "valid evaluator should compile: {:?}", result.err().map(|e| e.to_string()));
        let cr = result.unwrap();
        assert!(cr.so_path.exists(), ".so file must exist on disk");
        assert!(!cr.cached, "first compile should not be cached");
        assert!(cr.compile_ms > 0, "compile time should be positive");
    }

    #[test]
    fn test_compile_cache_hit() {
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        // First compile
        let first = compile_evaluator(user_code, dir.path())
            .expect("first compile must succeed");
        assert!(!first.cached);

        // Second compile — must be a cache hit
        let second = compile_evaluator(user_code, dir.path())
            .expect("second compile must succeed");
        assert!(second.cached, "second compile of same code must be cached");
        assert_eq!(second.compile_ms, 0, "cached compile must report 0ms");
        assert_eq!(first.so_path, second.so_path, "same hash → same .so path");
    }

    #[test]
    fn test_compile_invalid_code_returns_error() {
        let dir = TempDir::new().unwrap();
        // unsafe block is rejected by the validator
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    unsafe { Vec::new() }
}
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "code with unsafe must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("validation") || err.details.iter().any(|d| d.contains("unsafe")),
            "error must mention unsafe or validation: {}",
            err
        );
    }

    #[test]
    fn test_adjust_error_lines() {
        // Preamble is 10 lines; original error at line 15 should adjust to line 5
        let stderr = "error[E0425]: cannot find value\n  --> /tmp/abc.rs:15:5\n  |";
        let adjusted = adjust_error_lines(stderr, 10);
        let joined = adjusted.join("\n");
        assert!(joined.contains(":5:"), "line 15 - 10 preamble = line 5: got {}", joined);
        assert!(!joined.contains(":15:"), "original line 15 should be replaced");
    }

    #[test]
    fn test_adjust_error_lines_no_overflow() {
        // Preamble larger than line number → saturate at 0 (not panic)
        let stderr = "  --> /tmp/abc.rs:3:1";
        let adjusted = adjust_error_lines(stderr, 100);
        let joined = adjusted.join("\n");
        assert!(joined.contains(":0:"), "saturate_sub must produce 0: got {}", joined);
    }

    #[test]
    fn test_compile_missing_evaluate_node_fn() {
        let dir = TempDir::new().unwrap();
        // Valid syntax but missing evaluate_node function
        let user_code = r#"
fn helper() -> Vec<u8> { vec![] }
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "code without evaluate_node must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("evaluate_node"),
            "error must mention evaluate_node: {}",
            err.message
        );
    }

    #[test]
    fn test_rejects_evaluate_node_in_comment() {
        let dir = TempDir::new().unwrap();
        // The string "fn evaluate_node" appears only in a comment — no actual function
        let user_code = r#"
// fn evaluate_node is documented here
fn helper() -> Vec<u8> { vec![] }
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "evaluate_node only in comment must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("evaluate_node"),
            "error must mention evaluate_node: {}",
            err.message
        );
    }

    #[test]
    fn test_rejects_evaluate_node_in_string() {
        let dir = TempDir::new().unwrap();
        // The string "fn evaluate_node" appears only inside a string literal
        let user_code = r#"
fn helper() -> &'static str { "fn evaluate_node" }
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(result.is_err(), "evaluate_node only in string must be rejected");
        let err = result.unwrap_err();
        assert!(
            err.message.contains("evaluate_node"),
            "error must mention evaluate_node: {}",
            err.message
        );
    }

    #[test]
    fn test_compile_cache_respects_ttl() {
        // Compile once, then backdate compiled_at to 600s ago (beyond TTL=300).
        // Second compile must NOT return cached=true.
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        // First compile
        let first = compile_evaluator(user_code, dir.path())
            .expect("first compile must succeed");
        assert!(!first.cached, "first compile must not be cached");

        // Backdate compiled_at in the .meta file to simulate a stale entry.
        // Bug #1784: the seed path must be the REAL post-fix identity, not
        // the retired raw sha256_hex(user_code) key.
        let info = cache_identity_info_from_source(&assemble_evaluator_source(user_code));
        let meta_path = dir.path().join(format!("{}.meta", info.identity));
        let old_epoch = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            - 600; // 600s ago — beyond TTL of 300
        let stale_meta = cache::CacheMetadata {
            source_hash: info.source_hash.clone(),
            rustc_version: info.rustc_version.clone(),
            abi_version: info.abi_version,
            compiled_at: format!("{}s-since-epoch", old_epoch),
            compile_ms: 100,
        };
        cache::write_metadata(&meta_path, &stale_meta).expect("must write stale meta");

        // Second compile — stale meta must trigger recompile (cached=false)
        let second = compile_evaluator(user_code, dir.path())
            .expect("second compile must succeed");
        assert!(!second.cached, "stale cache entry must trigger recompile, not return cached=true");
    }

    // ---- Bug #1784: cache identity must cover PREAMBLE/EPILOGUE + ABI ----

    const SHA256_HEX_DIGEST_LEN: usize = 64;
    const TEST_ABI_VERSION_A: u64 = 1;
    const TEST_ABI_VERSION_B: u64 = 2;
    const TEST_RUSTC_VERSION: &str = "rustc 1.91.0";

    #[test]
    fn test_compute_cache_identity_deterministic() {
        let a = compute_cache_identity("some source", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
        let b = compute_cache_identity("some source", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
        assert_eq!(a, b, "identity must be deterministic for identical inputs");
        assert_eq!(a.len(), SHA256_HEX_DIGEST_LEN, "identity must be a SHA-256 hex digest");
    }

    #[test]
    fn test_compute_cache_identity_differs_on_abi_version_alone() {
        // Proves ABI version is an independent identity component, not
        // merely subsumed by hashing the source text: same source, same
        // rustc, only abi_version differs.
        let source = "identical assembled source text";
        let id_abi1 = compute_cache_identity(source, TEST_ABI_VERSION_A, TEST_RUSTC_VERSION);
        let id_abi2 = compute_cache_identity(source, TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
        assert_ne!(id_abi1, id_abi2, "identity must change when ONLY abi_version differs");
    }

    #[test]
    fn test_compute_cache_identity_differs_on_source_change() {
        // The core of Bug #1784: identity must be sensitive to the assembled
        // source (PREAMBLE + EPILOGUE), not just the raw user code.
        let id_a = compute_cache_identity("preamble-v1 + user code", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
        let id_b = compute_cache_identity("preamble-v2 + user code", TEST_ABI_VERSION_B, TEST_RUSTC_VERSION);
        assert_ne!(id_a, id_b, "identity must change when the assembled source text differs");
    }

    #[test]
    fn test_compute_cache_identity_differs_on_rustc_version() {
        let source = "identical assembled source text";
        let id_a = compute_cache_identity(source, TEST_ABI_VERSION_B, "rustc 1.91.0");
        let id_b = compute_cache_identity(source, TEST_ABI_VERSION_B, "rustc 1.92.0");
        assert_ne!(id_a, id_b, "identity must change when rustc_version differs");
    }

    #[test]
    fn test_assembled_source_embeds_correct_abi_version_via_substitution() {
        // Bug #1784 review MAJOR-3: XRAY_ABI_VERSION is now the ONE source
        // of truth -- PREAMBLE contains a placeholder token, never a
        // hardcoded literal duplicate. This test proves the substitution
        // performed by assemble_evaluator_source_with_preamble actually
        // happens (the real value is embedded) and never leaks the raw
        // placeholder token into compilable source.
        let assembled = assemble_evaluator_source(
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }",
        );
        assert!(
            assembled.contains(&format!("XRAY_ABI_VERSION: u64 = {};", XRAY_ABI_VERSION)),
            "assembled source must embed the current XRAY_ABI_VERSION value ({})",
            XRAY_ABI_VERSION
        );
        assert!(
            !assembled.contains(ABI_VERSION_PLACEHOLDER),
            "assembled source must not leak the raw placeholder token"
        );
    }

    #[test]
    fn test_compile_evaluator_cache_miss_when_preamble_changes() {
        // MANDATORY regression guard (Bug #1784 review MAJOR-1): proves,
        // through the REAL compile_evaluator pipeline (not just the
        // standalone compute_cache_identity() function), that changing
        // ONLY the preamble text -- user code, ABI version, and rustc
        // toolchain held fixed -- forces a fresh compile (cached == false),
        // never a stale-artifact reuse. This is the test class that would
        // catch a future regression where the cache identity is again
        // derived from raw user_code alone while the composite-identity
        // helper functions (compute_cache_identity / cache_identity_info_
        // from_source) are left in place unchanged.
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;

        let preamble_v1 = PREAMBLE;
        let first = compile_evaluator_with_preamble(user_code, dir.path(), preamble_v1)
            .expect("first compile (preamble v1) must succeed");
        assert!(!first.cached, "first compile must not be a cache hit");

        // Sanity check: repeating the SAME preamble + user code must hit the
        // local cache -- proves the harness itself actually exercises
        // caching, so the later cache-miss assertion is meaningful.
        let repeat = compile_evaluator_with_preamble(user_code, dir.path(), preamble_v1)
            .expect("repeat compile with unchanged preamble must succeed");
        assert!(repeat.cached, "repeat compile with unchanged preamble must be a cache hit");

        // preamble_v2 differs from preamble_v1 by a trivial appended comment
        // -- still valid, compilable Rust; the OwnedNode/EvalFinding/
        // debug_log definitions user code depends on are unchanged.
        let preamble_v2 = format!(
            "{}\n// preamble v2 marker (Bug #1784 regression test)\n",
            preamble_v1
        );
        let second = compile_evaluator_with_preamble(user_code, dir.path(), &preamble_v2)
            .expect("second compile (preamble v2) must succeed");
        assert!(
            !second.cached,
            "changing ONLY the preamble text must force a cache MISS, never reuse the preamble-v1 artifact"
        );
        assert_ne!(
            second.so_path, first.so_path,
            "preamble v1 and v2 artifacts must be stored under different identities"
        );
    }

    /// Test helper: write a fake `.so` + valid `.meta` pair at `{identity}.so`
    /// / `{identity}.meta` inside `dir`, so a cache lookup for `identity`
    /// finds a pre-existing artifact.
    fn seed_cache_entry(dir: &Path, identity: &str, so_bytes: &[u8], meta: &cache::CacheMetadata) {
        std::fs::write(dir.join(format!("{}.so", identity)), so_bytes).expect("seed .so");
        cache::write_metadata(&dir.join(format!("{}.meta", identity)), meta).expect("seed .meta");
    }

    #[test]
    fn test_stale_artifact_under_old_raw_user_code_hash_is_not_reused() {
        // MANDATORY regression guard (Bug #1784): before the fix, the cache
        // key was sha256_hex(user_code) alone. Seed an artifact at exactly
        // that pre-fix lookup path, with metadata pre-fix code treats as
        // fully valid, and prove the fix does not reuse it.
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        let old_buggy_key = sha256_hex(user_code);
        let garbage_bytes = b"NOT_A_REAL_SHARED_OBJECT_FROM_OLD_PREAMBLE";
        seed_cache_entry(
            dir.path(),
            &old_buggy_key,
            garbage_bytes,
            &cache::CacheMetadata {
                source_hash: old_buggy_key.clone(),
                rustc_version: cache::get_rustc_version(),
                abi_version: XRAY_ABI_VERSION,
                compiled_at: chrono_now_iso(),
                compile_ms: 5,
            },
        );

        let result = compile_evaluator(user_code, dir.path())
            .expect("compile must succeed despite the seeded stale artifact");

        let garbage_so_path = dir.path().join(format!("{}.so", old_buggy_key));
        assert!(!result.cached, "must NOT be a cache hit on an artifact keyed by the pre-fix scheme");
        assert_ne!(result.so_path, garbage_so_path, "must NOT return the seeded garbage file");
        let so_bytes = std::fs::read(&result.so_path).expect("compiled .so must exist");
        assert!(so_bytes.len() > garbage_bytes.len(), "a real compiled cdylib must be far larger than the garbage placeholder");
    }

    #[test]
    fn test_meta_abi_version_field_mismatch_at_matching_filename_forces_miss() {
        // Defense in depth: even if a .so/.meta pair sits at the CURRENT
        // identity's filename (so_path.exists() == true), a recorded
        // abi_version that doesn't match XRAY_ABI_VERSION must still force a
        // MISS. Guards the per-field freshness check independently of the
        // filename-based key.
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        let assembled = assemble_evaluator_source(user_code);
        let info = cache_identity_info_from_source(&assembled);
        let wrong_abi = info.abi_version.saturating_sub(1);
        assert_ne!(wrong_abi, info.abi_version);

        seed_cache_entry(
            dir.path(),
            &info.identity,
            b"GARBAGE_AT_RIGHT_FILENAME_WRONG_ABI",
            &cache::CacheMetadata {
                source_hash: info.source_hash.clone(),
                rustc_version: info.rustc_version.clone(),
                abi_version: wrong_abi,
                compiled_at: chrono_now_iso(),
                compile_ms: 5,
            },
        );

        let result = compile_evaluator(user_code, dir.path())
            .expect("compile must succeed despite the seeded stale-ABI metadata");
        assert!(!result.cached, "an abi_version field mismatch must force a MISS even at a matching filename");
    }

    // --- AC8: synchronous evaluator-mode classification ---

    /// AC8: "A scan provides evaluate_node OR graph mode — validated
    /// synchronously before job submission, not discovered at runtime."
    /// ADR-001: "A graph evaluator must not export evaluate_node; a
    /// legacy evaluator must not be treated as graph mode. The loader
    /// rejects a mixed or incomplete callback family." Six cases:
    /// legacy-only (Ok), graph-complete (Ok), both families mixed (Err),
    /// collect_facts-without-analyze_graph (Err), analyze_graph-without-
    /// collect_facts (Err), and neither present (Err).
    #[test]
    fn detect_evaluator_mode_classifies_legacy_graph_and_rejects_missing_or_mixed_families() {
        let legacy = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
        assert_eq!(detect_evaluator_mode(legacy).unwrap(), EvaluatorMode::Legacy);

        let graph = r#"
fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult { GraphResult::default() }
"#;
        assert_eq!(detect_evaluator_mode(graph).unwrap(), EvaluatorMode::Graph);

        let mixed = format!("{legacy}\n{graph}");
        assert!(detect_evaluator_mode(&mixed).is_err(), "mixed legacy+graph families must be rejected");

        let collect_only = "fn collect_facts(node: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> { Vec::new() }";
        assert!(detect_evaluator_mode(collect_only).is_err(), "collect_facts without analyze_graph must be rejected");

        let analyze_only = "fn analyze_graph(g: &CodeGraph, facts: &FactIndex) -> GraphResult { GraphResult::default() }";
        assert!(detect_evaluator_mode(analyze_only).is_err(), "analyze_graph without collect_facts must be rejected");

        let neither = "fn helper() -> i32 { 42 }";
        assert!(detect_evaluator_mode(neither).is_err(), "an evaluator with no recognized callback family must be rejected");
    }

    #[test]
    fn test_preamble_types_match_crate_types() {
        // Verify PREAMBLE contains the same fields as the real types.
        // This catches drift between preamble and crate definitions.
        assert!(PREAMBLE.contains("pub kind: String"), "OwnedNode.kind missing from preamble");
        assert!(PREAMBLE.contains("pub start_line: usize"), "OwnedNode.start_line missing");
        assert!(PREAMBLE.contains("pub start_byte: usize"), "OwnedNode.start_byte missing");
        assert!(PREAMBLE.contains("pub end_byte: usize"), "OwnedNode.end_byte missing");
        assert!(PREAMBLE.contains("pub children: Vec<OwnedNode>"), "OwnedNode.children missing");
        assert!(PREAMBLE.contains("pub is_named: bool"), "OwnedNode.is_named missing");
        // ABI v2: text is now a method, not a field
        assert!(PREAMBLE.contains("pub fn text("), "OwnedNode.text() method missing from preamble");
        assert!(PREAMBLE.contains("source: Arc<str>"), "OwnedNode.source field missing from preamble");
        assert!(PREAMBLE.contains("pub pattern: String"), "EvalFinding.pattern missing");
        assert!(PREAMBLE.contains("pub line: usize"), "EvalFinding.line missing");
        assert!(PREAMBLE.contains("pub snippet: String"), "EvalFinding.snippet missing");
    }

    // --- AC1/AC2/AC5: debug_log tests ---

    #[test]
    fn test_preamble_contains_debug_log() {
        // AC1: debug_log(msg) must be in preamble so user code can call it.
        assert!(
            PREAMBLE.contains("fn debug_log(msg: &str)"),
            "PREAMBLE must define fn debug_log(msg: &str)"
        );
        assert!(
            PREAMBLE.contains("thread_local!"),
            "PREAMBLE must define thread_local! storage for debug messages"
        );
        assert!(
            PREAMBLE.contains("RefCell"),
            "PREAMBLE must use RefCell for interior mutability of debug log"
        );
        assert!(
            PREAMBLE.contains("10240"),
            "PREAMBLE must enforce 10KB (10240 byte) size limit"
        );
    }

    #[test]
    fn test_epilogue_contains_drain_debug_log() {
        // AC2: xray_drain_debug_log must be exported so the loader can retrieve messages.
        assert!(
            EPILOGUE.contains("xray_drain_debug_log"),
            "EPILOGUE must export xray_drain_debug_log symbol"
        );
        assert!(
            EPILOGUE.contains("Vec<String>"),
            "xray_drain_debug_log must return Vec<String>"
        );
    }

    #[test]
    fn test_compiled_evaluator_can_call_debug_log() {
        // Integration: evaluator code using debug_log() must compile successfully.
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    debug_log("visiting node");
    debug_log(&format!("kind={}", node.kind));
    Vec::new()
}
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(
            result.is_ok(),
            "evaluator using debug_log must compile: {:?}",
            result.err().map(|e| e.to_string())
        );
    }

    #[test]
    fn test_debug_log_truncation_limits() {
        // AC5: evaluator calling debug_log 200 times must compile (truncation is runtime).
        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    for i in 0..200usize {
        debug_log(&format!("message {}", i));
    }
    Vec::new()
}
"#;
        let result = compile_evaluator(user_code, dir.path());
        assert!(
            result.is_ok(),
            "evaluator with looping debug_log must compile: {:?}",
            result.err().map(|e| e.to_string())
        );
    }

    #[test]
    fn test_debug_log_truncation_limits_runtime() {
        // Runtime assertion: evaluator calling debug_log 150 times must yield exactly
        // 100 messages (not 150) — the PREAMBLE enforces a hard cap of 100 per evaluation.
        use crate::dynlib::DynlibEvaluator;
        use crate::owned_node::OwnedNode;
        use crate::scanner::Evaluator;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    for i in 0..150usize {
        debug_log(&format!("msg {}", i));
    }
    Vec::new()
}
"#;
        let cr = compile_evaluator(user_code, dir.path())
            .expect("evaluator with 150 debug_log calls must compile");

        let evaluator = DynlibEvaluator::load(&cr.so_path)
            .expect("compiled .so must load successfully");

        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);

        evaluator.evaluate_node(&node);
        let messages = evaluator.drain_debug_log();

        assert_eq!(
            messages.len(),
            100,
            "100-message cap must be enforced at runtime: got {} messages",
            messages.len()
        );
        // Verify the FIRST 100 messages are retained (not arbitrary ones).
        for (i, message) in messages.iter().enumerate().take(100usize) {
            assert_eq!(
                message,
                &format!("msg {}", i),
                "message at index {} must be 'msg {}', got: {}",
                i, i, message
            );
        }
    }

    const CONCURRENT_TEST_1425_USER_CODE: &str = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    if node.kind == "try_statement" {
        findings.push(EvalFinding {
            pattern: "concurrent_test_1425".to_string(),
            line: node.start_line,
            snippet: String::new(),
        });
    }
    findings
}
"#;

    /// Spawns `thread_count` threads that all call `compile_evaluator` with the
    /// SAME `user_code` against the SAME `cache_dir` concurrently, returning
    /// every thread's result.
    fn spawn_concurrent_compiles(
        cache_dir: &Path,
        user_code: &str,
        thread_count: usize,
    ) -> Vec<Result<CompileResult, CompileError>> {
        let handles: Vec<_> = (0..thread_count)
            .map(|_| {
                let cache_dir = cache_dir.to_path_buf();
                let code = user_code.to_string();
                std::thread::spawn(move || compile_evaluator(&code, &cache_dir))
            })
            .collect();

        handles
            .into_iter()
            .map(|h| h.join().expect("compile_evaluator thread must not panic"))
            .collect()
    }

    /// Loads a compiled evaluator .so and verifies it produces the expected
    /// single finding for a `try_statement` node — proves the artifact is not
    /// merely present on disk but is a correct, loadable evaluator.
    fn assert_evaluator_finds_try_statement(so_path: &Path, index: usize) {
        use crate::dynlib::DynlibEvaluator;
        use crate::owned_node::OwnedNode;
        use crate::scanner::Evaluator;

        let evaluator = DynlibEvaluator::load(so_path)
            .unwrap_or_else(|e| panic!("compile #{}: .so must load successfully: {}", index, e));
        let node = OwnedNode::new_leaf_for_test("try_statement", "try { } catch { }", 1, true);
        let findings = evaluator.evaluate_node(&node);
        assert_eq!(
            findings.len(),
            1,
            "compile #{}: evaluator must find exactly one match, got {:?}",
            index,
            findings
        );
        assert_eq!(
            findings[0].pattern, "concurrent_test_1425",
            "compile #{}: finding pattern must match evaluator source",
            index
        );
    }

    /// Bug #1425: two concurrent compiles of the SAME evaluator hash against a
    /// cold cache must NOT clobber each other's rustc intermediate .rcgu.o
    /// object files. Reproduced by fanning out N threads that all call
    /// compile_evaluator() with byte-identical source against a shared,
    /// freshly-created (cold) cache_dir at the same time.
    ///
    /// Pre-fix, the losing thread's rustc invocation fails with "rust-lld:
    /// error: cannot open <hash>.rcgu.o: No such file or directory" because
    /// both invocations wrote their -o output (and thus their codegen-unit
    /// object files) directly into the shared cache_dir using the identical
    /// crate name (the hash). Post-fix, every thread must succeed AND produce
    /// a loadable, CORRECT compiled evaluator — not just "no panic".
    #[test]
    fn test_concurrent_compile_same_hash_cold_cache_both_succeed() {
        let dir = TempDir::new().unwrap();
        const THREAD_COUNT: usize = 8;
        let results =
            spawn_concurrent_compiles(dir.path(), CONCURRENT_TEST_1425_USER_CODE, THREAD_COUNT);

        for (i, result) in results.iter().enumerate() {
            assert!(
                result.is_ok(),
                "concurrent compile #{} of identical hash must succeed, got: {:?}",
                i,
                result.as_ref().err().map(|e| e.to_string())
            );
        }

        for (i, result) in results.into_iter().enumerate() {
            let cr = result.unwrap();
            assert!(
                cr.so_path.exists(),
                "compile #{}: .so file must exist on disk at {}",
                i,
                cr.so_path.display()
            );
            assert_evaluator_finds_try_statement(&cr.so_path, i);
        }
    }

    #[test]
    fn test_debug_log_byte_limit_runtime() {
        // Runtime assertion: when messages exceed 10KB total, further messages are
        // silently dropped — enforced by the PREAMBLE 10240-byte guard.
        // Each message is 200 bytes; 51 * 200 = 10200 <= 10240 (fits).
        // 52nd message would push total to 10400 > 10240 (dropped).
        use crate::dynlib::DynlibEvaluator;
        use crate::owned_node::OwnedNode;
        use crate::scanner::Evaluator;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let big_msg: String = std::iter::repeat('b').take(200).collect();
    for _ in 0..60usize {
        debug_log(&big_msg);
    }
    Vec::new()
}
"#;
        let cr = compile_evaluator(user_code, dir.path())
            .expect("evaluator with large debug_log messages must compile");

        let evaluator = DynlibEvaluator::load(&cr.so_path)
            .expect("compiled .so must load successfully");

        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);

        evaluator.evaluate_node(&node);
        let messages = evaluator.drain_debug_log();

        // 51 * 200 = 10200 bytes fits within 10240; 52nd message (200 bytes) would
        // make 10400 > 10240 and is dropped. Exactly 51 messages must be retained.
        assert_eq!(
            messages.len(),
            51,
            "10KB byte cap must be enforced: expected 51 messages, got {}",
            messages.len()
        );
        let expected_msg: String = "b".repeat(200);
        for (i, msg) in messages.iter().enumerate() {
            assert_eq!(
                msg, &expected_msg,
                "message {} must be the 200-byte string, got len={}",
                i,
                msg.len()
            );
        }
    }
}
