use crate::finding::EvalFinding;
use crate::owned_node::OwnedNode;
use crate::scanner::Evaluator;
use libloading::{Library, Symbol};
use std::path::Path;

type EvaluateNodeFn = fn(&OwnedNode) -> Vec<EvalFinding>;
/// Bug #1855 (H1): `extern "C"` -- this is the FIRST symbol the loader
/// calls, before compatibility between host and evaluator has been
/// established, so it cannot rely on the plain Rust ABI happening to match
/// (that match is exactly the precondition this probe exists to prove).
/// Contrast with `EvaluateNodeFn`/`DrainDebugLogFn` above/below, which are
/// data-carrying callbacks that only ever run AFTER this probe succeeds and
/// legitimately keep the plain Rust ABI.
type AbiVersionFn = extern "C" fn() -> u64;
type DrainDebugLogFn = fn() -> Vec<String>;
/// The evaluator `.so` exports its embedded rustc version as a raw
/// pointer/length pair of plain scalars. This avoids crossing the dynamic
/// library boundary with a Rust-owned String or Vec before compatibility has
/// been established.
///
/// Bug #1855 (H1): `extern "C"` for the same reason as `AbiVersionFn` above
/// -- these two are read immediately after `xray_abi_version` and still
/// before rustc-version compatibility itself has been confirmed.
type RustcVersionPtrFn = extern "C" fn() -> u64;
type RustcVersionLenFn = extern "C" fn() -> u64;

const HOST_RUSTC_VERSION: &str = env!("CIDX_HOST_RUSTC_VERSION");

fn verify_rustc_version_match(host: &str, evaluator: &str) -> Result<(), String> {
    if host == evaluator {
        Ok(())
    } else {
        Err(format!(
            "rustc version mismatch: host binary was built with '{}', but evaluator .so was built with '{}'. Recompile the evaluator with the host toolchain.",
            host, evaluator
        ))
    }
}

unsafe fn evaluator_rustc_version(lib: &Library) -> Result<String, String> {
    let ptr_fn: Symbol<RustcVersionPtrFn> = lib
        .get(b"xray_rustc_version_ptr")
        .map_err(|e| format!("Symbol xray_rustc_version_ptr not found: {}", e))?;
    let len_fn: Symbol<RustcVersionLenFn> = lib
        .get(b"xray_rustc_version_len")
        .map_err(|e| format!("Symbol xray_rustc_version_len not found: {}", e))?;
    let ptr = ptr_fn();
    let len = len_fn();
    if ptr == 0 {
        return Err("Evaluator rustc version export returned a null pointer".to_string());
    }
    if len == 0 || len > 4096 {
        return Err(format!(
            "Evaluator rustc version export returned invalid length {}",
            len
        ));
    }
    let bytes = std::slice::from_raw_parts(ptr as *const u8, len as usize);
    let version = std::str::from_utf8(bytes)
        .map_err(|e| format!("Evaluator rustc version export is not valid UTF-8: {}", e))?;
    Ok(version.to_owned())
}

fn verify_loaded_rustc_version(lib: &Library) -> Result<(), String> {
    let evaluator_version = unsafe { evaluator_rustc_version(lib)? };
    verify_rustc_version_match(HOST_RUSTC_VERSION, &evaluator_version)
}

pub struct DynlibEvaluator {
    _lib: Library,
    evaluate_fn: EvaluateNodeFn,
    /// Optional drain function — None when loading an old .so that predates debug_log.
    drain_debug_log_fn: Option<DrainDebugLogFn>,
}

impl std::fmt::Debug for DynlibEvaluator {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DynlibEvaluator").finish()
    }
}

impl DynlibEvaluator {
    pub fn load(so_path: &Path) -> Result<Self, String> {
        let lib = unsafe {
            Library::new(so_path)
                .map_err(|e| format!("Failed to load {}: {}", so_path.display(), e))?
        };

        // Verify ABI version before trusting the evaluate function pointer.
        let abi_version: u64 = unsafe {
            let sym: Symbol<AbiVersionFn> = lib
                .get(b"xray_abi_version")
                .map_err(|e| format!("Symbol xray_abi_version not found: {}", e))?;
            sym()
        };
        // Bug #1784 review MAJOR-3: reads crate::compiler::XRAY_ABI_VERSION
        // directly -- the ONE source of truth -- instead of declaring an
        // independent EXPECTED_ABI_VERSION copy that could drift from it.
        if abi_version != crate::compiler::XRAY_ABI_VERSION {
            return Err(format!(
                "ABI version mismatch: evaluator has version {} but loader expects {}. \
                 Recompile your evaluator.",
                abi_version,
                crate::compiler::XRAY_ABI_VERSION
            ));
        }

        verify_loaded_rustc_version(&lib)?;

        let evaluate_fn: EvaluateNodeFn = unsafe {
            let sym: Symbol<EvaluateNodeFn> = lib
                .get(b"xray_evaluate_node")
                .map_err(|e| format!("Symbol xray_evaluate_node not found: {}", e))?;
            *sym
        };

        // Load xray_drain_debug_log — optional for backward compat with old .so files.
        // Missing symbol is non-fatal: drain_debug_log() returns empty vec in that case.
        let drain_debug_log_fn: Option<DrainDebugLogFn> = unsafe {
            lib.get::<DrainDebugLogFn>(b"xray_drain_debug_log")
                .ok()
                .map(|sym| *sym)
        };

        Ok(Self { _lib: lib, evaluate_fn, drain_debug_log_fn })
    }

    /// Drain accumulated debug_log() messages from the evaluator's thread-local buffer.
    ///
    /// Returns all messages collected since the last drain (or since load), then clears
    /// the buffer. Returns empty vec when the evaluator made no debug_log() calls or
    /// when the loaded .so predates the debug_log feature.
    pub fn drain_debug_log(&self) -> Vec<String> {
        match self.drain_debug_log_fn {
            Some(f) => f(),
            None => vec![],
        }
    }
}

impl Evaluator for DynlibEvaluator {
    fn evaluate_node(&self, node: &OwnedNode) -> Vec<EvalFinding> {
        (self.evaluate_fn)(node)
    }

    fn drain_debug_log(&self) -> Vec<String> {
        // Use fully-qualified call to route to the inherent method, not this trait
        // method (which would recurse).  The inherent method dispatches to the dynlib's
        // xray_drain_debug_log export, or returns empty vec for old .so files.
        DynlibEvaluator::drain_debug_log(self)
    }
}

// SAFETY: The evaluator function loaded from the dynlib is a pure function because
// the validator (validator.rs) enforces:
// 1. No `unsafe` blocks or functions
// 2. No `static` or `static mut` (no shared mutable state)
// 3. No std::fs, std::net, std::process, std::env, std::io access
// 4. No extern blocks or raw pointers
// 5. No println!/eprintln!/print!/eprint! (no I/O side effects)
// 6. debug_log() uses thread_local! storage (RefCell<Vec<String>>), which is
//    per-thread and does not create shared mutable state across threads.
// The function takes &OwnedNode (immutable ref) and returns Vec<EvalFinding> (owned).
// With no global state and no I/O, concurrent calls from rayon threads are safe.
// The Library handle (_lib) is kept alive for the lifetime of the evaluator,
// ensuring the function pointer remains valid.
unsafe impl Send for DynlibEvaluator {}
unsafe impl Sync for DynlibEvaluator {}

/// ADR-002 Defect 1 fix: returns `Option<Vec<UserFact>>` -- `None` means
/// the dylib's OWN `catch_unwind` (see `GRAPH_EPILOGUE` in `compiler.rs`)
/// caught a panic inside `collect_facts` before it could ever try to cross
/// this dylib boundary, mirroring `AnalyzeGraphFn`'s doc comment below
/// exactly (both epilogue exports now follow the identical shape).
type CollectFactsFn = fn(&OwnedNode, &str) -> Option<Vec<crate::graph::user_facts::UserFact>>;
/// Returns `Option<GraphResult>` -- `None` means the dylib's OWN
/// `catch_unwind` (see `GRAPH_EPILOGUE` in `compiler.rs`) caught a panic
/// inside `analyze_graph` before it could ever try to cross this dylib
/// boundary. This loader never needs its own `catch_unwind` around the
/// call: by the time control returns here, the dylib has already reduced
/// "succeeded" vs "panicked" to a plain, already-safe value.
type AnalyzeGraphFn =
    fn(&crate::graph::csr::handle::GraphHandle, &crate::graph::user_facts::FactsHandle) -> Option<crate::graph::analyze::result::GraphResult>;

/// Story #1792 (S3, AC1): returns `Option<Vec<EvalFinding>>` -- `None`
/// means the dylib's OWN `catch_unwind` (see `GRAPH_REFINE_EPILOGUE` in
/// `compiler.rs`) caught a panic inside `refine` before it could ever try
/// to cross this dylib boundary, mirroring `AnalyzeGraphFn`'s doc comment
/// exactly. OPTIONAL: a graph-mode evaluator with no `fn refine` has no
/// such symbol at all, distinct from a panic.
type RefineFn = fn(
    &OwnedNode,
    &crate::graph::refine::FileContext,
    &crate::graph::csr::handle::GraphHandle,
    &crate::graph::user_facts::FactsHandle,
) -> Option<Vec<EvalFinding>>;

/// Story #1787 AC8: loads a GRAPH-MODE compiled evaluator, distinct from
/// `DynlibEvaluator` (legacy-only). Mirrors `DynlibEvaluator::load`'s ABI
/// verification exactly, then resolves `xray_collect_facts`/
/// `xray_analyze_graph`/`xray_refine` the SAME optional-symbol way
/// `xray_drain_debug_log` already is -- a missing symbol is a legitimate
/// outcome (a legacy-mode `.so`, or a graph-mode one with no `fn refine`,
/// loaded here has none), reported via
/// `has_collect_facts`/`has_analyze_graph`/`has_refine`, never a load
/// failure.
pub struct GraphDynlibEvaluator {
    _lib: Library,
    collect_facts_fn: Option<CollectFactsFn>,
    analyze_graph_fn: Option<AnalyzeGraphFn>,
    refine_fn: Option<RefineFn>,
}

impl std::fmt::Debug for GraphDynlibEvaluator {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("GraphDynlibEvaluator").finish()
    }
}

impl GraphDynlibEvaluator {
    pub fn load(so_path: &Path) -> Result<Self, String> {
        let lib = unsafe {
            Library::new(so_path).map_err(|e| format!("Failed to load {}: {}", so_path.display(), e))?
        };

        let abi_version: u64 = unsafe {
            let sym: Symbol<AbiVersionFn> = lib
                .get(b"xray_abi_version")
                .map_err(|e| format!("Symbol xray_abi_version not found: {}", e))?;
            sym()
        };
        if abi_version != crate::compiler::XRAY_ABI_VERSION {
            return Err(format!(
                "ABI version mismatch: evaluator has version {} but loader expects {}. \
                 Recompile your evaluator.",
                abi_version,
                crate::compiler::XRAY_ABI_VERSION
            ));
        }

        verify_loaded_rustc_version(&lib)?;

        let collect_facts_fn: Option<CollectFactsFn> =
            unsafe { lib.get::<CollectFactsFn>(b"xray_collect_facts").ok().map(|sym| *sym) };
        let analyze_graph_fn: Option<AnalyzeGraphFn> =
            unsafe { lib.get::<AnalyzeGraphFn>(b"xray_analyze_graph").ok().map(|sym| *sym) };
        let refine_fn: Option<RefineFn> =
            unsafe { lib.get::<RefineFn>(b"xray_refine").ok().map(|sym| *sym) };

        Ok(Self { _lib: lib, collect_facts_fn, analyze_graph_fn, refine_fn })
    }

    /// AC7/AC8: distinguishes "the loaded artifact exports analyze_graph"
    /// from "not requested" (the caller's own concern, upstream of this
    /// loader) and from "not exported" (`false` here) -- never inferred
    /// from a failed call.
    pub fn has_analyze_graph(&self) -> bool {
        self.analyze_graph_fn.is_some()
    }

    pub fn has_collect_facts(&self) -> bool {
        self.collect_facts_fn.is_some()
    }

    /// Story #1792 (S3, AC1): distinguishes "the loaded artifact exports
    /// xray_refine" (a graph-mode evaluator defining `fn refine`) from
    /// "not exported" (`false` here, the common case: refine is OPTIONAL).
    pub fn has_refine(&self) -> bool {
        self.refine_fn.is_some()
    }

    /// Calls the loaded evaluator's `analyze_graph`, if exported. The
    /// OUTER `Option` distinguishes "not exported" (`None`, AC7's
    /// `Absent` case) from "exported" (`Some(..)`); the INNER `Option`
    /// (only meaningful when outer is `Some`) distinguishes "panicked"
    /// (`None`, caught by the dylib's own `catch_unwind` in
    /// `GRAPH_EPILOGUE`) from "succeeded" (`Some(result)`). No
    /// `catch_unwind` is needed HERE: the panic never crosses this
    /// dylib boundary at all.
    pub fn call_analyze_graph(
        &self,
        g: &crate::graph::csr::handle::GraphHandle,
        facts: &crate::graph::user_facts::FactsHandle,
    ) -> Option<Option<crate::graph::analyze::result::GraphResult>> {
        self.analyze_graph_fn.map(|f| f(g, facts))
    }

    /// Mirrors `call_analyze_graph`'s two-level Option contract exactly:
    /// outer `None` = not exported, outer `Some(inner)` = exported, where
    /// inner `None` = the dylib's own catch_unwind (GRAPH_EPILOGUE) caught
    /// a panic, inner `Some(facts)` = succeeded.
    pub fn call_collect_facts(
        &self,
        node: &OwnedNode,
        file: &str,
    ) -> Option<Option<Vec<crate::graph::user_facts::UserFact>>> {
        self.collect_facts_fn.map(|f| f(node, file))
    }

    /// Story #1792 (S3, AC1): calls the loaded evaluator's `refine`, if
    /// exported. Mirrors `call_analyze_graph`'s exact two-level Option
    /// contract: OUTER `None` = not exported, outer `Some(inner)` =
    /// exported, where inner `None` = the dylib's own `catch_unwind`
    /// (`GRAPH_REFINE_EPILOGUE`) caught a panic, inner `Some(findings)` =
    /// succeeded. No `catch_unwind` needed HERE -- the panic never crosses
    /// this dylib boundary at all.
    pub fn call_refine(
        &self,
        node: &OwnedNode,
        ctx: &crate::graph::refine::FileContext,
        g: &crate::graph::csr::handle::GraphHandle,
        facts: &crate::graph::user_facts::FactsHandle,
    ) -> Option<Option<Vec<EvalFinding>>> {
        self.refine_fn.map(|f| f(node, ctx, g, facts))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::analyze::ReduceFinding;

    /// RED phase (AC8): `GraphDynlibEvaluator` does not exist yet. This
    /// proves what its GREEN implementation must do -- load a LEGACY-mode
    /// compiled evaluator and report `has_analyze_graph() == false` /
    /// `has_collect_facts() == false` (the "not exported" case, distinct
    /// from "not requested"), and load a GRAPH-mode compiled evaluator and
    /// report both `true`.
    #[test]
    fn graph_dynlib_evaluator_detects_presence_of_graph_mode_exports() {
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let legacy_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        let legacy_cr = compiler::compile_evaluator(legacy_code, dir.path()).expect("legacy must compile");
        let legacy_evaluator = GraphDynlibEvaluator::load(&legacy_cr.so_path).expect("legacy .so must load");
        assert!(!legacy_evaluator.has_analyze_graph(), "legacy .so must not export xray_analyze_graph");
        assert!(!legacy_evaluator.has_collect_facts(), "legacy .so must not export xray_collect_facts");

        let graph_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"#;
        let graph_cr = compiler::compile_evaluator(graph_code, dir.path()).expect("graph mode must compile");
        let graph_evaluator = GraphDynlibEvaluator::load(&graph_cr.so_path).expect("graph .so must load");
        assert!(graph_evaluator.has_analyze_graph(), "graph .so must export xray_analyze_graph");
        assert!(graph_evaluator.has_collect_facts(), "graph .so must export xray_collect_facts");
    }

    /// Builds a tiny real `CodeGraph` (A -> B) and an empty `FactIndex` for
    /// exercising `call_analyze_graph` against a REAL compiled dylib.
    fn small_graph_and_facts() -> (crate::graph::csr::CodeGraph, crate::graph::user_facts::FactIndex) {
        use crate::graph::csr::builder::CodeGraphBuilder;
        use crate::graph::csr::candidate::Candidate;
        use crate::graph::identity::make_symbol_id;
        use crate::graph::reasons;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(1, 1));
        builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::SAME_FILE)]);
        (builder.build(), crate::graph::user_facts::FactIndex::new())
    }

    /// Story #1792 (S3, AC3/AC4): a genuinely CROSS-FILE graph -- A (file 1)
    /// calls B (file 2), and B carries a cached AC2 signature line. This is
    /// the fixture the signature-captioning test below needs: unlike
    /// `small_graph_and_facts` (both symbols in file 1, via `SAME_FILE`),
    /// this proves captioning works when the flagged definition genuinely
    /// lives in a DIFFERENT file than the call site.
    fn two_file_graph_with_cached_signature() -> crate::graph::csr::CodeGraph {
        use crate::graph::csr::builder::CodeGraphBuilder;
        use crate::graph::csr::candidate::Candidate;
        use crate::graph::identity::make_symbol_id;
        use crate::graph::reasons;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(1);
        let a = builder.intern_symbol(make_symbol_id(1, 0));
        let b = builder.intern_symbol(make_symbol_id(2, 0));
        builder.add_signature(b, "run()".to_string());
        builder.add_reference(a, 1, 1, 0, &[Candidate::new(b, reasons::UNIQUE_NAME_IN_REPO)]);
        builder.build()
    }

    /// THE AC3/AC4 discriminating proof: a real compiled `analyze_graph`
    /// resolves A's callee (B, declared in a DIFFERENT file), captions the
    /// finding with B's CACHED signature via the new `g.signature_for`
    /// accessor, and never adds B to `result.refine`. This is exactly the
    /// mechanism that "stops RefineSet approaching whole-repo size on path
    /// queries" (AC3): with `signature_for` available, an evaluator has no
    /// need to request a second, per-file look at B just to caption it.
    #[test]
    fn analyze_graph_captions_a_cross_file_symbol_via_signature_for_without_entering_refine_set() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let graph = two_file_graph_with_cached_signature();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    for callee in g.callees_of(0) {
        let symbol = g.resolve_symbol(callee).expect("callee came from g.callees_of, always valid");
        let signature = g.signature_for(callee).unwrap_or("<no signature>").to_string();
        result.findings.push(ReduceFinding {
            pattern: "cross-file-caption".to_string(),
            message: "captioned without re-parsing".to_string(),
            involved: vec![symbol],
            signatures: vec![signature],
        });
        // Deliberately NEVER pushed to result.refine -- signature_for
        // already supplied everything needed to caption this finding.
    }
    result
}
"#;
        let evaluator = compile_and_load_graph(user_code, dir.path());
        let result = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("analyze_graph IS exported -- outer must be Some(..)")
            .expect("a real graph-mode evaluator calling a real accessor must not panic");

        assert_eq!(result.findings.len(), 1, "must have captioned the one cross-file call site");
        assert_eq!(
            result.findings[0].signatures,
            vec!["run()".to_string()],
            "the finding must carry B's cached signature, retrieved WITHOUT re-parsing file 2"
        );
        assert!(
            result.refine.is_empty(),
            "B must never enter the RefineSet -- signature_for made a second per-file look unnecessary"
        );
    }

    /// Compiles `user_code` into `dir` and loads it as a
    /// `GraphDynlibEvaluator` -- shared setup for the three
    /// `call_analyze_graph` discrimination tests below.
    fn compile_and_load_graph(user_code: &str, dir: &std::path::Path) -> GraphDynlibEvaluator {
        let cr = crate::compiler::compile_evaluator(user_code, dir).expect("must compile");
        GraphDynlibEvaluator::load(&cr.so_path).expect("must load")
    }

    /// AC8's central invariant, case 1 of 3: a real graph-mode evaluator
    /// calling a REAL `GraphHandle` accessor must succeed with the CORRECT
    /// `GraphResult` -- proves the `Some(Some(result))` arm of
    /// `call_analyze_graph`'s `Option<Option<GraphResult>>` contract.
    #[test]
    fn call_analyze_graph_succeeds_with_a_real_accessor_call() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let (graph, facts) = small_graph_and_facts();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let success_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    for callee in g.callees_of(0) {
        result.refine.push(g.resolve_symbol(callee).expect("callee came from g.callees_of, always valid"));
    }
    result
}
"#;
        let evaluator = compile_and_load_graph(success_code, dir.path());
        let result = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("analyze_graph IS exported -- outer must be Some(..)")
            .expect("a real graph-mode evaluator calling a real accessor must not panic");
        assert_eq!(result.refine.len(), 1, "callees_of(0) must find exactly the A->B edge");
        assert_eq!(result.refine[0], graph.resolve_symbol(1), "must resolve to B's real SymbolId");
    }

    /// Case 2 of 3: a legacy-mode evaluator has no `analyze_graph` to call
    /// at all -- proves the OUTER `None` arm, distinct from a successful
    /// empty result (`Some(Some(empty))`).
    #[test]
    fn call_analyze_graph_is_outer_none_when_not_exported() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let (graph, facts) = small_graph_and_facts();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let legacy_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
        let evaluator = compile_and_load_graph(legacy_code, dir.path());
        assert!(
            evaluator.call_analyze_graph(&graph_handle, &facts_handle).is_none(),
            "calling analyze_graph on a legacy-mode evaluator (no export) must be the outer \
             None, never a successful Some(Some(empty)) result"
        );
    }

    /// Case 3 of 3: a genuinely panicking `analyze_graph` (triggered via
    /// `.unwrap()` on `None` -- `panic!` itself is banned by
    /// `validator.rs`) must be caught INSIDE the dylib and reported as the
    /// inner `Some(None)`, never crashing the test process.
    #[test]
    fn call_analyze_graph_catches_a_genuine_panic_inside_the_dylib() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let (graph, facts) = small_graph_and_facts();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let panic_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let boom: Option<i32> = None;
    boom.unwrap();
    GraphResult::default()
}
"#;
        let evaluator = compile_and_load_graph(panic_code, dir.path());
        let outer = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("must be the outer Some(..) -- analyze_graph IS exported, the panic is caught inside");
        assert!(
            outer.is_none(),
            "a panic inside analyze_graph must be caught inside the dylib, reported as Some(None), never a crash"
        );
    }

    /// Defect 1 (ADR-002 fix): mirrors `call_analyze_graph_is_outer_none_when_not_exported`'s
    /// exact shape for `call_collect_facts` -- a legacy-mode evaluator has
    /// no `collect_facts` at all, which must be the OUTER `None`, distinct
    /// from a successful empty result (`Some(Some(vec![]))`).
    #[test]
    fn call_collect_facts_is_outer_none_when_not_exported() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let legacy_code = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }";
        let evaluator = compile_and_load_graph(legacy_code, dir.path());
        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);
        assert!(
            evaluator.call_collect_facts(&node, "file.rs").is_none(),
            "calling collect_facts on a legacy-mode evaluator (no export) must be the outer \
             None, never a successful Some(Some(empty)) result"
        );
    }

    /// Defect 1 (ADR-002 fix), the discriminating companion to the panic
    /// test below: a REAL, non-panicking `collect_facts` that legitimately
    /// finds nothing must report `Some(Some(vec![]))` -- never confused
    /// with the panic case's `Some(None)`. Without this test, a buggy
    /// implementation that always returns the inner `None` regardless of
    /// whether a panic occurred could still pass the panic test alone.
    #[test]
    fn call_collect_facts_succeeds_with_a_real_non_panicking_call() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let success_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"#;
        let evaluator = compile_and_load_graph(success_code, dir.path());
        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);
        let outer = evaluator
            .call_collect_facts(&node, "file.rs")
            .expect("collect_facts IS exported -- outer must be Some(..)");
        assert_eq!(
            outer,
            Some(Vec::new()),
            "a real, non-panicking collect_facts finding nothing must report Some(Some(vec![])), \
             never confused with the panic case's Some(None)"
        );
    }

    /// Defect 1 (ADR-002 fix): a genuinely panicking `collect_facts` (no
    /// `catch_unwind` today -- this is the UB the fix eliminates) must be
    /// caught INSIDE the dylib and reported as the inner `Some(None)`,
    /// never crashing the process and never confused with the empty-success
    /// case proven above.
    #[test]
    fn call_collect_facts_catches_a_genuine_panic_inside_the_dylib() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let panic_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    let boom: Option<i32> = None;
    boom.unwrap();
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    GraphResult::default()
}
"#;
        let evaluator = compile_and_load_graph(panic_code, dir.path());
        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);
        let outer = evaluator
            .call_collect_facts(&node, "file.rs")
            .expect("must be the outer Some(..) -- collect_facts IS exported, the panic is caught inside");
        assert!(
            outer.is_none(),
            "a panic inside collect_facts must be caught inside the dylib, reported as Some(None), never a crash"
        );
    }

    /// Defect 2 (ADR-002 fix), full-surface proof: ALL 7 `GraphHandle`
    /// accessors, called with out-of-range/extreme input from inside a
    /// REAL compiled `analyze_graph`, must never panic -- proving the whole
    /// accessor surface is safe under adversarial input, not just the two
    /// (`resolve_symbol`/`resolve_string`) that were changed. `resolve_symbol`/
    /// `resolve_string` now return `Option`, so the evaluator uses
    /// `.is_none()`/`.unwrap_or(...)` rather than `.unwrap()` -- unwrapping
    /// `None` inside the evaluator would itself panic (caught by the
    /// dylib's own catch_unwind), which would make this test pass for the
    /// WRONG reason.
    #[test]
    fn all_seven_graph_handle_accessors_survive_adversarial_input_without_panicking() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let (graph, facts) = small_graph_and_facts();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let adversarial_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let _callees = g.callees_of(u32::MAX);
    let _callers = g.callers_of(u32::MAX);
    let _reachable = g.reachable_from(&[u32::MAX], 10);
    let _shortest = g.shortest_path_to_any(u32::MAX, &[u32::MAX], 10);
    let _scc = g.strongly_connected_components();
    let symbol_was_none = g.resolve_symbol(u32::MAX).is_none();
    let string_was_none = g.resolve_string(u32::MAX).is_none();
    if symbol_was_none && string_was_none {
        result.findings.push(ReduceFinding::default());
    }
    result
}
"#;
        let evaluator = compile_and_load_graph(adversarial_code, dir.path());
        let outer = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("analyze_graph IS exported -- outer must be Some(..)");
        let result = outer.expect(
            "the whole GraphHandle accessor surface must survive adversarial (out-of-range) \
             input without panicking -- Some(None) here would mean something still panicked",
        );
        assert_eq!(
            result.findings.len(),
            1,
            "resolve_symbol/resolve_string must both have returned None for the out-of-range id, \
             proving they discriminate gracefully rather than panicking"
        );
    }

    /// Shared setup for the `call_refine`/`has_refine` tests below -- a
    /// real two-file graph (with B's cached signature), an empty
    /// `FactIndex`, a leaf `OwnedNode`, and a `FileContext` naming the file
    /// under refine. Returns owned values (never the handles themselves,
    /// which borrow from `graph`/`facts` and cannot outlive this function)
    /// so each call site builds its own `GraphHandle`/`FactsHandle` locally.
    fn refine_test_fixture() -> (
        crate::graph::csr::CodeGraph,
        crate::graph::user_facts::FactIndex,
        OwnedNode,
        crate::graph::refine::FileContext,
    ) {
        let graph = two_file_graph_with_cached_signature();
        let facts = crate::graph::user_facts::FactIndex::new();
        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);
        let ctx = crate::graph::refine::FileContext { file: "src/A.java".to_string() };
        (graph, facts, node, ctx)
    }

    /// Story #1792 (S3, AC1): `has_refine()` must distinguish "the loaded
    /// artifact exports xray_refine" (a graph-mode evaluator defining
    /// `fn refine`) from "not exported" (one that does not) -- mirroring
    /// `has_analyze_graph`/`has_collect_facts`'s exact contract.
    #[test]
    fn graph_dynlib_evaluator_detects_presence_of_refine_export() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();

        let without_refine = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
        let evaluator = compile_and_load_graph(without_refine, dir.path());
        assert!(!evaluator.has_refine(), "an evaluator with no fn refine must not export xray_refine");

        let with_refine = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> { Vec::new() }
"#;
        let evaluator = compile_and_load_graph(with_refine, dir.path());
        assert!(evaluator.has_refine(), "an evaluator defining fn refine must export xray_refine");
    }

    /// AC1's central invariant: a real compiled `refine` callback, given
    /// the file's `OwnedNode`, a `FileContext` naming the file, and the
    /// SAME `GraphHandle`/`FactsHandle` accessor ABI `analyze_graph`
    /// already uses -- including calling the REAL `g.signature_for`
    /// accessor -- must run to completion and return real `EvalFinding`s.
    /// Proves `call_refine`'s `Some(Some(result))` arm end-to-end through a
    /// REAL dylib call, not a stub, and that the accessor ABI genuinely
    /// works from inside `refine` (not just `analyze_graph`).
    #[test]
    fn call_refine_succeeds_with_a_real_accessor_call() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let (graph, facts, node, ctx) = refine_test_fixture();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let user_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    let signature = g.signature_for(1).unwrap_or("<no signature>");
    vec![EvalFinding {
        pattern: "refine-ran".to_string(),
        line: node.start_line,
        snippet: format!("{}:{}", ctx.file, signature),
    }]
}
"#;
        let evaluator = compile_and_load_graph(user_code, dir.path());
        let outer = evaluator
            .call_refine(&node, &ctx, &graph_handle, &facts_handle)
            .expect("refine IS exported -- outer must be Some(..)");
        let findings = outer.expect("a real, non-panicking refine must not panic");
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].pattern, "refine-ran");
        assert_eq!(
            findings[0].snippet, "src/A.java:run()",
            "the FileContext's file AND the real g.signature_for(1) call must both reach refine"
        );
    }

    /// Builds a `(GraphHandle, FactsHandle)` pair borrowing from `graph`/
    /// `facts` -- deduplicates the two-line handle-construction pair the
    /// `call_refine` tests below would otherwise each repeat.
    fn refine_handles<'g, 'f>(
        graph: &'g crate::graph::csr::CodeGraph,
        facts: &'f crate::graph::user_facts::FactIndex,
    ) -> (crate::graph::csr::handle::GraphHandle<'g>, crate::graph::user_facts::FactsHandle<'f>) {
        (crate::graph::csr::handle::GraphHandle::from_graph(graph), crate::graph::user_facts::FactsHandle::from_facts(facts))
    }

    /// Mirrors `call_analyze_graph_is_outer_none_when_not_exported`'s exact
    /// shape: an evaluator with no `fn refine` has nothing to call at all,
    /// which must be the OUTER `None`, distinct from a successful empty
    /// result (`Some(Some(vec![]))`).
    #[test]
    fn call_refine_is_outer_none_when_not_exported() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let (graph, facts, node, ctx) = refine_test_fixture();
        let (graph_handle, facts_handle) = refine_handles(&graph, &facts);

        let without_refine = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
"#;
        let evaluator = compile_and_load_graph(without_refine, dir.path());
        assert!(
            evaluator.call_refine(&node, &ctx, &graph_handle, &facts_handle).is_none(),
            "calling refine on an evaluator with no export must be the outer None, never Some(Some(empty))"
        );
    }

    /// A genuinely panicking `refine` (triggered via `.unwrap()` on `None`)
    /// must be caught INSIDE the dylib (Rule 13) and reported as the inner
    /// `Some(None)`, never crashing the test process -- mirrors
    /// `call_analyze_graph_catches_a_genuine_panic_inside_the_dylib` exactly.
    #[test]
    fn call_refine_catches_a_genuine_panic_inside_the_dylib() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let (graph, facts, node, ctx) = refine_test_fixture();
        let (graph_handle, facts_handle) = refine_handles(&graph, &facts);

        let panic_code = r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }
fn refine(node: &OwnedNode, ctx: &FileContext, g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> Vec<EvalFinding> {
    let boom: Option<i32> = None;
    boom.unwrap();
    Vec::new()
}
"#;
        let evaluator = compile_and_load_graph(panic_code, dir.path());
        let outer = evaluator
            .call_refine(&node, &ctx, &graph_handle, &facts_handle)
            .expect("must be the outer Some(..) -- refine IS exported, the panic is caught inside");
        assert!(outer.is_none(), "a panic inside refine must be caught inside the dylib, reported as Some(None), never a crash");
    }

    #[test]
    fn test_load_nonexistent_so_returns_error() {
        let result = DynlibEvaluator::load(Path::new("/tmp/nonexistent_xray_test.so"));
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.contains("Failed to load"), "error: {}", err);
    }

    #[test]
    fn test_abi_version_matches_expected() {
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    vec![]
}
"#;
        let cr = compiler::compile_evaluator(user_code, dir.path())
            .expect("compile must succeed");

        // Load should succeed only when abi version matches compiler::XRAY_ABI_VERSION
        let evaluator = DynlibEvaluator::load(&cr.so_path);
        assert!(evaluator.is_ok(), "load must succeed with matching ABI version: {:?}", evaluator.err());
    }

    /// Bug #1855 (Layer 1): this is the pure, discriminating half of the
    /// mismatch guard -- no compiled `.so` needed -- proving the intended
    /// comparison accepts identical host/evaluator rustc version strings.
    /// Discriminating together with the rejection test immediately below:
    /// this one alone proves nothing (a stub that always returns `Ok`
    /// would pass it too).
    #[test]
    fn verify_rustc_version_match_accepts_identical_versions() {
        let version = "rustc 1.98.0 (aaaaaaaaa 2026-01-01)";
        let result = verify_rustc_version_match(version, version);
        assert!(
            result.is_ok(),
            "identical host/evaluator rustc versions must be accepted: {:?}",
            result.err()
        );
    }

    /// Bug #1855 (Layer 1, RED phase): proves the comparison actually
    /// REJECTS a divergent host/evaluator pairing -- the double-free
    /// scenario from the mission repro (host 1.91.0, evaluator .so
    /// 1.98.0) -- and that the resulting diagnostic names BOTH versions,
    /// per acceptance criterion 5 and mandatory rule 4. A guard that
    /// merely logs a warning or silently accepts is a FAIL, not a pass
    /// with a caveat (Messi Rule 2/13).
    #[test]
    fn verify_rustc_version_match_rejects_divergent_versions_naming_both() {
        let host_version = "rustc 1.91.0 (bbbbbbbbb 2025-10-28)";
        let evaluator_version = "rustc 1.98.0 (ccccccccc 2026-06-01)";
        let result = verify_rustc_version_match(host_version, evaluator_version);
        assert!(
            result.is_err(),
            "divergent host/evaluator rustc versions must be rejected"
        );
        let err = result.unwrap_err();
        assert!(
            err.contains(host_version),
            "error must name the HOST rustc version so a developer can diagnose: {}",
            err
        );
        assert!(
            err.contains(evaluator_version),
            "error must name the EVALUATOR .so's rustc version so a developer can diagnose: {}",
            err
        );
    }

    #[test]
    fn test_compiled_evaluator_exports_abi_version_matching_single_source_of_truth() {
        // Bug #1784 review MAJOR-3: after centralizing XRAY_ABI_VERSION to
        // compiler::XRAY_ABI_VERSION as the ONE definition (PREAMBLE text is
        // generated from it at assemble-time via ABI_VERSION_PLACEHOLDER
        // substitution, and this loader reads it directly rather than
        // declaring its own copy), there is no second constant left to
        // drift. This test proves the compiled .so's OWN exported
        // xray_abi_version() symbol -- the actual runtime value baked into
        // the artifact by a REAL compile -- equals compiler::XRAY_ABI_VERSION
        // end-to-end, not just "both constants happen to read 2 today".
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        let cr = compiler::compile_evaluator(user_code, dir.path())
            .expect("compile must succeed");

        let lib = unsafe { Library::new(&cr.so_path) }.expect("must load compiled .so directly");
        let abi_version: u64 = unsafe {
            let sym: Symbol<AbiVersionFn> = lib
                .get(b"xray_abi_version")
                .expect("xray_abi_version symbol must exist on a freshly compiled evaluator");
            sym()
        };
        assert_eq!(
            abi_version,
            compiler::XRAY_ABI_VERSION,
            "compiled evaluator's exported ABI version must equal the single source of truth"
        );
    }

    /// Bug #1855 (Layer 1): mirrors
    /// `test_compiled_evaluator_exports_abi_version_matching_single_source_of_truth`
    /// exactly: compiles a REAL evaluator through the real pipeline, loads
    /// the REAL `.so` directly, and proves its exported rustc version
    /// equals `cache::get_rustc_version()` -- the existing pinned-toolchain
    /// probe that already feeds `compute_cache_identity` -- not merely
    /// that a source-text placeholder was substituted somewhere. Reading
    /// the two symbols as `u64` scalars (never a `String`/`Vec<u8>`) keeps
    /// this call safe to make on a `.so` that has NOT yet been proven to
    /// share the host's rustc, which is exactly the state this loader is
    /// in immediately after `Library::new` succeeds.
    #[test]
    fn test_compiled_evaluator_exports_rustc_version_matching_single_source_of_truth() {
        use crate::cache;
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        let cr = compiler::compile_evaluator(user_code, dir.path()).expect("compile must succeed");

        let lib = unsafe { Library::new(&cr.so_path) }.expect("must load compiled .so directly");
        let exported_version = unsafe {
            super::evaluator_rustc_version(&lib)
                .expect("freshly compiled evaluator must expose a valid rustc version")
        };

        assert_eq!(
            exported_version,
            cache::get_rustc_version(),
            "compiled evaluator's exported rustc version must equal the pinned-toolchain probe \
             (cache::get_rustc_version) -- the single source of truth the evaluator was actually \
             compiled under"
        );
    }

    #[test]
    fn test_load_and_evaluate_compiled_evaluator() {
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    if node.kind == "test_node" {
        vec![EvalFinding {
            pattern: "dynlib-test".to_string(),
            line: node.start_line,
            snippet: "test".to_string(),
        }]
    } else {
        vec![]
    }
}
"#;
        let result = compiler::compile_evaluator(user_code, dir.path());
        assert!(result.is_ok(), "compile failed: {:?}", result.err());
        let cr = result.unwrap();

        let evaluator = DynlibEvaluator::load(&cr.so_path);
        assert!(evaluator.is_ok(), "load failed: {:?}", evaluator.err());
        let evaluator = evaluator.unwrap();

        let node = OwnedNode::new_leaf_for_test("test_node", "test", 42, true);
        let findings = evaluator.evaluate_node(&node);
        assert_eq!(findings.len(), 1);
        assert_eq!(findings[0].pattern, "dynlib-test");
        assert_eq!(findings[0].line, 42);
    }

    // --- AC2: debug_log drain tests ---

    #[test]
    fn test_drain_debug_log_returns_messages() {
        // AC2: evaluator calling debug_log produces messages retrievable via drain_debug_log.
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    debug_log("hello from evaluator");
    debug_log(&format!("kind is: {}", node.kind));
    Vec::new()
}
"#;
        let cr = compiler::compile_evaluator(user_code, dir.path())
            .expect("compile must succeed");
        let evaluator = DynlibEvaluator::load(&cr.so_path)
            .expect("load must succeed");

        let node = OwnedNode::new_leaf_for_test("some_node", "x", 1, true);
        evaluator.evaluate_node(&node);
        let messages = evaluator.drain_debug_log();
        assert_eq!(messages.len(), 2, "must have 2 debug messages: {:?}", messages);
        assert_eq!(messages[0], "hello from evaluator");
        assert_eq!(messages[1], "kind is: some_node");
    }

    #[test]
    fn test_drain_debug_log_empty_without_calls() {
        // AC6: when evaluator makes no debug_log calls, drain returns empty vec.
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    Vec::new()
}
"#;
        let cr = compiler::compile_evaluator(user_code, dir.path())
            .expect("compile must succeed");
        let evaluator = DynlibEvaluator::load(&cr.so_path)
            .expect("load must succeed");

        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);
        evaluator.evaluate_node(&node);
        let messages = evaluator.drain_debug_log();
        assert!(messages.is_empty(), "must be empty when no debug_log calls: {:?}", messages);
    }

    #[test]
    fn test_drain_debug_log_via_trait_dispatch() {
        // Regression: drain_debug_log must work through Evaluator trait dispatch,
        // not just as an inherent method on DynlibEvaluator.
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    debug_log("trait dispatch test");
    Vec::new()
}
"#;
        let cr = compiler::compile_evaluator(user_code, dir.path())
            .expect("compile must succeed");
        let evaluator = DynlibEvaluator::load(&cr.so_path)
            .expect("load must succeed");

        let node = OwnedNode::new_leaf_for_test("root", "", 1, true);

        // Call through trait reference — this is how scanner.rs uses evaluators.
        let eval_ref: &dyn Evaluator = &evaluator;
        eval_ref.evaluate_node(&node);
        let messages = eval_ref.drain_debug_log();
        assert_eq!(messages.len(), 1, "trait dispatch must return debug messages: {:?}", messages);
        assert_eq!(messages[0], "trait dispatch test");
    }

    /// Depth well past the empirically measured 8,000-12,000 SIGABRT cliff
    /// (issue #1795). Builds an iterative (non-recursive) chain of `depth`
    /// "wrapper" nodes around one "needle" leaf, exactly mirroring
    /// `owned_node.rs`'s own `deep_chain` test helper. Built via
    /// `OwnedNode::new_leaf_for_test`/`new_node_for_test` (Bug #1791: the
    /// `source` field is private, so this module -- a sibling of
    /// `owned_node`, not a descendant -- can no longer construct `OwnedNode`
    /// via a raw struct literal). Each node gets its own freshly-allocated
    /// `Arc<str>` holding the same "needle" text the original struct-literal
    /// version shared via `Arc::clone`; that sharing was an incidental
    /// optimization, not something this stack-depth test depends on.
    fn deep_ffi_chain(depth: usize) -> OwnedNode {
        let mut node = OwnedNode::new_leaf_for_test("needle", "needle", depth + 1, true);
        for level in (0..depth).rev() {
            node = OwnedNode::new_node_for_test(
                "wrapper",
                "needle",
                level + 1,
                0,
                0,
                vec![node],
                true,
            );
        }
        node
    }

    /// Bug #1795: `has_descendant_of_kind`/`collect_descendants_of_kind` are
    /// mirrored as a string literal into the evaluator PREAMBLE
    /// (compiler.rs), so user evaluators inherit whatever stack-safety
    /// property the mirror has. This test compiles a REAL evaluator that
    /// calls `descendants_of_kind` (delegating to
    /// `collect_descendants_of_kind`), builds a 50,000-level `OwnedNode`
    /// chain via `deep_ffi_chain`, and passes it by reference across the FFI
    /// boundary into the compiled `.so`. The evaluator's own call stack (a
    /// separate compiled artifact, not `xray-core`'s) is what is under test.
    ///
    /// Pre-fix: SIGABRTs inside the compiled evaluator, proving the PREAMBLE
    /// mirror has the identical cliff as core `owned_node.rs` — fixing only
    /// the core crate does NOT protect user evaluators.
    #[test]
    fn test_preamble_mirror_survives_deep_nesting_across_ffi() {
        use crate::compiler;
        use tempfile::TempDir;

        let dir = TempDir::new().unwrap();
        let user_code = r#"
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    node.descendants_of_kind("needle").iter().map(|d| EvalFinding {
        pattern: "found".to_string(),
        line: d.start_line,
        snippet: d.text().to_string(),
    }).collect()
}
"#;
        let cr = compiler::compile_evaluator(user_code, dir.path()).expect("compile must succeed");
        let evaluator = DynlibEvaluator::load(&cr.so_path).expect("load must succeed");

        const DEPTH: usize = 50_000;
        let node = deep_ffi_chain(DEPTH);

        // Exercises the PREAMBLE mirror across the FFI boundary. Pre-fix:
        // SIGABRTs inside the compiled evaluator.
        let findings = evaluator.evaluate_node(&node);
        assert_eq!(findings.len(), 1, "must find exactly the one needle leaf through 50,000 levels");
        assert_eq!(findings[0].line, DEPTH + 1);

        // This test's subject is the PREAMBLE mirror, not core OwnedNode's
        // own Drop glue (dedicated regression test in owned_node.rs).
        // `forget` avoids conflating that separately-tested site here.
        std::mem::forget(node);
    }

    /// Shared host-side setup/execution for the two Bug #1816 reproducer
    /// tests below -- builds the real `small_graph_and_facts` fixture,
    /// compiles+loads `user_code` as a real dylib, and calls
    /// `analyze_graph`, returning the outer `Option`. Kept out of the two
    /// `#[test]` functions themselves so each can stay focused on naming
    /// its own call order and evaluator source.
    fn run_bug_1816_reproducer(
        user_code: &str,
        dir: &std::path::Path,
    ) -> Option<Option<crate::graph::analyze::result::GraphResult>> {
        let (graph, facts) = small_graph_and_facts();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);
        let evaluator = compile_and_load_graph(user_code, dir);
        evaluator.call_analyze_graph(&graph_handle, &facts_handle)
    }

    /// Bounded BFS depth used by both Bug #1816 reproducer evaluators below
    /// -- an arbitrary but generous ceiling for the tiny 2-symbol fixture
    /// graph (`small_graph_and_facts`), matching the bug report's own
    /// minimal reproducer exactly so these tests reproduce the SAME shape
    /// that was observed crashing, not merely a same-symptom variant.
    const BUG_1816_MAX_DEPTH: usize = 20;

    /// Bug #1816 RED-phase reproducer, order A: `signature_for` called
    /// before `shortest_path_to_any` in the SAME `analyze_graph` evaluator.
    /// Per the bug report this combination corrupts the heap
    /// (`free(): double free detected in tcache 2`, SIGABRT) even though
    /// each accessor alone runs clean over every symbol in a loop. This
    /// test is deliberately run in ISOLATION (see the module doc note in
    /// the fix commit) because a SIGABRT/SIGSEGV inside `cargo test`'s
    /// single shared process would take down every other test running
    /// concurrently in the same binary.
    #[test]
    fn bug_1816_signature_for_then_shortest_path_to_any_does_not_corrupt_the_heap() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();

        let user_code = format!(
            r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{ Vec::new() }}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let _ = g.signature_for(0).is_some();
    let t: Vec<u32> = Vec::new();
    let _ = g.shortest_path_to_any(0u32, &t, {BUG_1816_MAX_DEPTH});
    GraphResult::default()
}}
"#
        );
        let outer = run_bug_1816_reproducer(&user_code, dir.path());
        assert!(
            outer.expect("analyze_graph IS exported -- outer must be Some(..)").is_some(),
            "signature_for followed by shortest_path_to_any must not corrupt the heap or panic"
        );
    }

    /// Bug #1816 RED-phase reproducer, order B: the reverse call order --
    /// `shortest_path_to_any` before `signature_for`. The bug report notes
    /// order only changes which signal is raised (SIGSEGV here vs SIGABRT
    /// for order A), not whether corruption happens, so both orders are
    /// required as separate regression tests.
    #[test]
    fn bug_1816_shortest_path_to_any_then_signature_for_does_not_corrupt_the_heap() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();

        let user_code = format!(
            r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {{ Vec::new() }}
fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {{
    let t: Vec<u32> = Vec::new();
    let _ = g.shortest_path_to_any(0u32, &t, {BUG_1816_MAX_DEPTH});
    let _ = g.signature_for(0).is_some();
    GraphResult::default()
}}
"#
        );
        let outer = run_bug_1816_reproducer(&user_code, dir.path());
        assert!(
            outer.expect("analyze_graph IS exported -- outer must be Some(..)").is_some(),
            "shortest_path_to_any followed by signature_for must not corrupt the heap or panic"
        );
    }

    /// Number of distinct `GraphHandle` accessors the pairwise evaluator
    /// source (`pairwise_accessor_evaluator_source`) exercises -- kept as
    /// its own constant so the host-side assertion in the test below can
    /// name the expected call count without duplicating the literal `9`.
    const PAIRWISE_ACCESSOR_COUNT: usize = 9;

    /// Evaluator source for the Bug #1816 pairwise regression test below:
    /// numbers all 9 `GraphHandle` accessors 0..9 (a plain `u32` kind rather
    /// than an `enum` -- the evaluator security validator forbids
    /// `#[derive]` attributes, and `Accessor` would need `Clone`/`Copy` to
    /// be indexed out of an array repeatedly, so a bare integer dispatched
    /// via `match` sidesteps that without weakening the validator), then
    /// calls EVERY ordered pair (9*9=81 pairs) inside a single
    /// `analyze_graph` invocation, reporting the total call count so the
    /// host can assert none were skipped. Split out from the `#[test]`
    /// itself purely to keep that function short and scannable.
    fn pairwise_accessor_evaluator_source() -> &'static str {
        r#"
const ACCESSOR_COUNT: u32 = 9;
const PAIRWISE_MAX_DEPTH: usize = 20;

fn call_accessor(g: &GraphHandle<'_>, kind: u32, sym: u32, targets: &Vec<u32>) -> usize {
    match kind {
        0 => { let _ = g.resolve_symbol(sym); 1 }
        1 => { let _ = g.signature_for(sym); 1 }
        2 => { let _ = g.is_symbol_referenced(sym); 1 }
        3 => { let _ = g.is_definitely_dead_code(sym); 1 }
        4 => { let _ = g.callers_of(sym); 1 }
        5 => { let _ = g.callees_of(sym); 1 }
        6 => { let _ = g.strongly_connected_components(); 1 }
        7 => { let _ = g.shortest_path_to_any(sym, targets, PAIRWISE_MAX_DEPTH); 1 }
        _ => { let _ = g.reachable_from(targets, PAIRWISE_MAX_DEPTH); 1 }
    }
}

fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let targets: Vec<u32> = vec![0u32, 1u32];
    let mut calls: usize = 0;
    for i in 0..ACCESSOR_COUNT {
        for j in 0..ACCESSOR_COUNT {
            calls += call_accessor(g, i, 0u32, &targets);
            calls += call_accessor(g, j, 0u32, &targets);
        }
    }
    let mut result = GraphResult::default();
    result.findings.push(ReduceFinding {
        pattern: "pairwise_probe".to_string(),
        message: format!("calls={}", calls),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    result
}
"#
    }

    /// Bug #1816, PAIRWISE regression coverage: per-accessor tests (like
    /// `all_seven_graph_handle_accessors_survive_adversarial_input_without_panicking`
    /// above) ALL PASSED while this bug was live -- single-accessor
    /// coverage is provably insufficient, since the corruption only
    /// appeared when TWO specific accessors were combined. This test
    /// compiles ONE real evaluator (`pairwise_accessor_evaluator_source`)
    /// that, inside a single `analyze_graph` call, invokes every ORDERED
    /// pair across all 9 `GraphHandle` accessors against a REAL built
    /// `CodeGraph` (via `two_file_graph_with_cached_signature`, which --
    /// unlike the bare `small_graph_and_facts` fixture -- has a real cached
    /// signature and a real cross-file reference, so
    /// `signature_for`/`is_symbol_referenced` exercise genuine data, not
    /// just the `None`/`false` early-return paths). A single compiled `.so`
    /// making 9*9=81 sequential accessor calls through the real FFI
    /// boundary is both more efficient and MORE aggressive than 81 separate
    /// single-pair binaries -- it also mirrors how a real multi-use-case
    /// evaluator (see the six-use-case fixture used for requirement #3)
    /// actually calls many accessors in sequence within one `analyze_graph`
    /// invocation.
    #[test]
    fn all_ordered_pairs_of_graph_accessors_run_clean_against_a_real_compiled_dylib() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let graph = two_file_graph_with_cached_signature();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let evaluator = compile_and_load_graph(pairwise_accessor_evaluator_source(), dir.path());
        let outer = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("analyze_graph IS exported -- outer must be Some(..)");
        let result = outer.expect(
            "every ordered pair of GraphHandle accessors must run clean through a real compiled \
             dylib -- Some(None) here would mean a panic or heap corruption was caught/observed",
        );
        assert_eq!(
            result.findings[0].message,
            format!("calls={}", 2 * PAIRWISE_ACCESSOR_COUNT * PAIRWISE_ACCESSOR_COUNT),
            "must have executed exactly 2 accessor calls for every one of the 9x9 ordered pairs"
        );
    }

    /// Bug #1816, requirement #3: the FULL six-use-case `analyze_graph`
    /// evaluator (conservative dead-code safety, unreferenced symbols,
    /// layering violations, package cycles, endpoint-to-sink reachability,
    /// blast radius) must complete
    /// without crashing. Embedded verbatim -- this is the SAME evaluator
    /// already manually verified end to end through the real `xray-cli`
    /// CLI, run from OUTSIDE `rust/` (the condition that reproduced the
    /// original bug), against the real 16-file `xray-graph-fixture` Java
    /// repo (see the bug's investigation notes): `--build-graph` then three
    /// consecutive `--analyze-graph` runs all exited 0 with sane findings
    /// (UC1's conservative dead-code safety probe, UC3 layering, UC4 cycles,
    /// UC5 reachability + negative control) after this fix, and reliably crashed with
    /// `free(): double free detected in tcache 2` before it.
    ///
    /// This `cargo test` version cannot reproduce that ABI-mismatch
    /// condition directly (the test binary's own `rustc` invocations always
    /// run from inside `rust/`, so toolchain resolution is self-consistent
    /// even on the pre-fix code -- see `cache::pinned_toolchain_channel`'s
    /// doc comment), so it is a FUNCTIONAL regression test: it proves the
    /// full evaluator's logic is correct end to end against a real compiled
    /// dylib and a real, deliberately layered/cyclic/reachable fixture
    /// graph -- every one of the six use cases must actually fire, not just
    /// "the process didn't crash".
    #[test]
    fn six_use_case_evaluator_runs_end_to_end_without_crashing() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let graph = six_use_case_test_graph();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let evaluator = compile_and_load_graph(six_use_case_evaluator_source(), dir.path());
        let outer = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("analyze_graph IS exported -- outer must be Some(..)");
        let result = outer.expect(
            "the full six-use-case evaluator must complete without crashing or panicking -- \
             Some(None) here would mean the dylib's own catch_unwind caught a panic",
        );

        let patterns: Vec<&str> = result.findings.iter().map(|f| f.pattern.as_str()).collect();
        // Story #1835 AC6: `neverCalled` is now fixture-marked PRIVATE, so
        // UC1 must restore a REAL dead-code finding for it -- the true
        // positive Bug #1833's fix gave up on -- instead of the merely
        // suppressed/undecidable finding this test asserted pre-#1835.
        assert!(
            result.findings.iter().any(|f| f.pattern == "uc1_dead_code" && f.message.contains("neverCalled")),
            "UC1 must report a real dead-code finding for the unreferenced PRIVATE neverCalled \
             symbol: {patterns:?}"
        );
        assert!(
            !result.findings.iter().any(|f| f.pattern == "uc1_dead_code_suppressed" && f.message.contains("neverCalled")),
            "neverCalled now has real visibility evidence, so it must NOT fall into the \
             suppressed/undecidable branch any more: {patterns:?}"
        );
        // AC4's dual-direction spirit, restated at this real-dylib
        // integration level too: an unreferenced PUBLIC symbol in the SAME
        // graph must stay suppressed, never a false dead-code claim.
        assert!(
            !result.findings.iter().any(|f| f.pattern == "uc1_dead_code" && f.message.contains("publicApi")),
            "UC1 must NOT claim the unreferenced PUBLIC publicApiMethod is definitely dead: {patterns:?}"
        );
        assert!(
            result.findings.iter().any(|f| f.pattern == "uc1_dead_code_suppressed" && f.message.contains("publicApi")),
            "UC1 must still report its conservative suppressed decision for the unreferenced \
             PUBLIC publicApiMethod: {patterns:?}"
        );
        assert!(
            result.findings.iter().any(|f| f.pattern == "uc2_unreferenced" && f.message.contains("neverCalled")),
            "the unreferenced neverCalled symbol must be flagged as UC2 unreferenced: {patterns:?}"
        );
        assert!(patterns.contains(&"uc3_layering_violation"), "controller->repository layering violation must fire: {patterns:?}");
        assert!(patterns.contains(&"uc4_cycle"), "the 2-node cycle must be reported as a strongly connected component: {patterns:?}");
        assert!(patterns.contains(&"uc5_endpoint_reaches_sink"), "the deleteUser endpoint must reach the rawDelete sink: {patterns:?}");
        assert!(patterns.contains(&"uc5_control_evaluated"), "the ping health-check negative control must be evaluated: {patterns:?}");
        assert!(
            !patterns.contains(&"uc5_control_UNEXPECTED_PATH"),
            "the ping health-check control must NOT reach any sink: {patterns:?}"
        );
        assert!(patterns.contains(&"uc6_blast_radius"), "the once-named symbol must be flagged for blast radius: {patterns:?}");
    }

    /// Builds the synthetic fixture graph `six_use_case_evaluator_runs_end_to_end_without_crashing`
    /// exercises: an `OrderController` (dense 0) that calls straight into
    /// `OrderRepository` (dense 2, a real UC3 layering violation), a
    /// `deleteUserAccount` endpoint (dense 1, signature contains
    /// "deleteUser") that calls `rawDeleteRow` (dense 3, signature contains
    /// "rawDelete" -- a real UC5 endpoint-to-sink path), a `pingCheck`
    /// health endpoint (dense 4, signature contains "ping") with NO
    /// outgoing edges at all (the UC5 negative control -- must reach no
    /// sink), a `getOnce` cache accessor (dense 5, signature contains
    /// "once" -- UC6 blast radius), an unreferenced, PRIVATE `neverCalled`
    /// symbol (dense 6, Story #1835's restored real UC1 dead-code
    /// finding/UC2 unreferenced), a genuine 2-node cycle (dense 7 <-> 8,
    /// UC4), and an unreferenced, PUBLIC `publicApiMethod` symbol (dense
    /// 9, UC1's still-conservative suppressed finding -- the AC4
    /// discriminating counterpart to `neverCalled`, restated here at the
    /// real-dylib level).
    fn six_use_case_test_graph() -> crate::graph::csr::CodeGraph {
        use crate::graph::csr::builder::CodeGraphBuilder;
        use crate::graph::csr::candidate::Candidate;
        use crate::graph::extract::local_index::{DeclarationKind, Visibility};
        use crate::graph::identity::make_symbol_id;
        use crate::graph::reasons;

        let mut builder = CodeGraphBuilder::with_candidate_capacity(6);
        let order_controller = builder.intern_symbol(make_symbol_id(1, 0));
        let delete_user_account = builder.intern_symbol(make_symbol_id(1, 1));
        let order_repository = builder.intern_symbol(make_symbol_id(2, 0));
        let raw_delete_row = builder.intern_symbol(make_symbol_id(2, 1));
        let ping_check = builder.intern_symbol(make_symbol_id(3, 0));
        let get_once = builder.intern_symbol(make_symbol_id(4, 0));
        let never_called = builder.intern_symbol(make_symbol_id(5, 0));
        let cycle_a = builder.intern_symbol(make_symbol_id(6, 0));
        let cycle_b = builder.intern_symbol(make_symbol_id(6, 1));
        // Story #1835 AC6: a second unreferenced symbol, PUBLIC this time,
        // so UC1's restored real finding and its still-live conservative
        // suppression both fire in the SAME graph -- the discriminating
        // pair AC4 requires, restated at this real-dylib level.
        let public_api_symbol = builder.intern_symbol(make_symbol_id(7, 0));

        builder.add_signature(order_controller, "class OrderController".to_string());
        builder.add_signature(delete_user_account, "deleteUserAccount() deleteUser".to_string());
        builder.add_signature(order_repository, "class OrderRepository".to_string());
        builder.add_signature(raw_delete_row, "rawDeleteRow() rawDelete".to_string());
        builder.add_signature(ping_check, "pingCheck() ping".to_string());
        builder.add_signature(get_once, "getOnce() once".to_string());
        builder.add_signature(never_called, "neverCalled()".to_string());
        builder.add_signature(cycle_a, "methodA()".to_string());
        builder.add_signature(cycle_b, "methodB()".to_string());
        builder.add_signature(public_api_symbol, "publicApiMethod()".to_string());
        // Story #1835: `neverCalled` is a PRIVATE helper -- provably not
        // externally visible, so unreferenced really does mean dead.
        // `publicApiMethod` is PUBLIC -- exactly jsoup's Connection/
        // Response shape (Bug #1833) -- so it must stay undecidable.
        builder.add_visibility(never_called, Visibility::Private);
        builder.add_visibility(public_api_symbol, Visibility::Public);
        // Bug #1858: `is_definitely_dead_code` now requires tracked-kind
        // evidence (Method/Type) before it will even consult visibility --
        // real production always attaches this via
        // `budget_bind::intern_declarations_and_attach_signatures`. Every
        // symbol in this fixture is narratively a class or a method (never
        // a field/constant), so attach the matching kind for each so this
        // fixture keeps representing real declared code.
        builder.add_kind(order_controller, DeclarationKind::Type);
        builder.add_kind(delete_user_account, DeclarationKind::Method);
        builder.add_kind(order_repository, DeclarationKind::Type);
        builder.add_kind(raw_delete_row, DeclarationKind::Method);
        builder.add_kind(ping_check, DeclarationKind::Method);
        builder.add_kind(get_once, DeclarationKind::Method);
        builder.add_kind(never_called, DeclarationKind::Method);
        builder.add_kind(cycle_a, DeclarationKind::Method);
        builder.add_kind(cycle_b, DeclarationKind::Method);
        builder.add_kind(public_api_symbol, DeclarationKind::Method);

        // UC3: controller calls repository directly.
        builder.add_reference(order_controller, 1, 1, 0, &[Candidate::new(order_repository, reasons::SAME_FILE)]);
        // UC5 positive: endpoint reaches the sink in one hop.
        builder.add_reference(delete_user_account, 1, 2, 0, &[Candidate::new(raw_delete_row, reasons::SAME_FILE)]);
        // UC4: a genuine 2-node cycle.
        builder.add_reference(cycle_a, 6, 1, 0, &[Candidate::new(cycle_b, reasons::SAME_FILE)]);
        builder.add_reference(cycle_b, 6, 2, 0, &[Candidate::new(cycle_a, reasons::SAME_FILE)]);

        for referenced in [order_repository, raw_delete_row, cycle_a, cycle_b] {
            builder.mark_referenced(referenced);
        }
        // ping_check deliberately has NO outgoing references -- the UC5
        // negative control -- and never_called/public_api_symbol are
        // deliberately never referenced at all -- the UC1 real-finding and
        // still-conservative-suppression fixture pair, and UC2's
        // unreferenced-symbol demonstration.
        let _ = ping_check;
        let _ = never_called;
        let _ = public_api_symbol;

        builder.build()
    }

    /// Verbatim copy of the six-use-case `analyze_graph` evaluator used by
    /// `six_use_case_evaluator_runs_end_to_end_without_crashing` -- kept in
    /// sync manually with the scratch copy this bug's investigation used
    /// for the real CLI/real-repo run (see that test's doc comment).
    fn six_use_case_evaluator_source() -> &'static str {
        r#"
fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {
    Vec::new()
}

// Exact enumeration of the graph's dense ids.
fn enumerate_ids(g: &GraphHandle<'_>) -> Vec<u32> {
    let mut ids: Vec<u32> = Vec::new();
    let mut i: u32 = 0;
    while (i as usize) < g.symbol_count() {
        ids.push(i);
        i += 1;
    }
    ids
}

// Explicit signature lookup. Deliberately returns None rather than "" so a
// failed lookup can be COUNTED and REPORTED -- an empty-string substitution
// would silently fail every .contains() probe and fake a passing control.
fn sig_of(g: &GraphHandle<'_>, d: u32) -> Option<String> {
    match g.signature_for(d) {
        Some(s) => Some(s.to_string()),
        None => None,
    }
}

fn flag(pattern: &str, message: String, sym: u64, sig: String) -> ReduceFinding {
    ReduceFinding {
        pattern: pattern.to_string(),
        message,
        involved: vec![sym],
        signatures: vec![sig],
    }
}

// UC1 conservative dead-code safety, UC2 unreferenced, UC6 blast radius.
fn uc1_uc2_uc6(g: &GraphHandle<'_>, ids: &Vec<u32>) -> Vec<ReduceFinding> {
    let mut out: Vec<ReduceFinding> = Vec::new();
    let mut missing: usize = 0;
    for idx in 0..ids.len() {
        let d = ids[idx];
        let sym = match g.resolve_symbol(d) {
            Some(s) => s,
            None => continue,
        };
        let sig = match sig_of(g, d) {
            Some(s) => s,
            None => {
                missing += 1;
                String::from("<no-signature>")
            }
        };
        // Story #1835: is_definitely_dead_code now returns Some(true) for a
        // symbol that is BOTH unreferenced AND provably not externally
        // visible (Java `private`) -- a real, restored UC1 finding. Every
        // other unreferenced case (Public/Protected/Unknown visibility)
        // stays the conservative suppressed/undecidable finding UC1 has
        // reported since Bug #1833: the graph cannot prove those are
        // unreachable from outside the repository.
        if !g.is_symbol_referenced(d) {
            if g.is_definitely_dead_code(d) == Some(true) {
                out.push(flag("uc1_dead_code", sig.clone(), sym, sig.clone()));
            } else {
                out.push(flag("uc1_dead_code_suppressed", sig.clone(), sym, sig.clone()));
            }
            out.push(flag("uc2_unreferenced", sig.clone(), sym, sig.clone()));
        }
        if sig.contains("format") || sig.contains("once") {
            let n = g.callers_of(d).len();
            let msg = format!("callers={} sig={}", n, sig);
            out.push(flag("uc6_blast_radius", msg, sym, sig.clone()));
        }
    }
    if missing > 0 {
        out.push(ReduceFinding {
            pattern: "uc0_missing_signatures".to_string(),
            message: format!("{} symbols lacked a signature; text probes unreliable", missing),
            involved: Vec::new(),
            signatures: Vec::new(),
        });
    }
    out
}

// UC3: an api-layer Controller calling a data-layer Repository directly.
fn uc3_layering(g: &GraphHandle<'_>, ids: &Vec<u32>) -> Vec<ReduceFinding> {
    let mut out: Vec<ReduceFinding> = Vec::new();
    for idx in 0..ids.len() {
        let d = ids[idx];
        let sig = match sig_of(g, d) {
            Some(s) => s,
            None => continue,
        };
        if !sig.contains("Controller") {
            continue;
        }
        let sym = match g.resolve_symbol(d) {
            Some(s) => s,
            None => continue,
        };
        let callees = g.callees_of(d);
        for ci in 0..callees.len() {
            let c = callees[ci];
            let csig = match sig_of(g, c) {
                Some(s) => s,
                None => continue,
            };
            if csig.contains("Repository") {
                if let Some(csym) = g.resolve_symbol(c) {
                    out.push(ReduceFinding {
                        pattern: "uc3_layering_violation".to_string(),
                        message: format!("{} -> {}", sig, csig),
                        involved: vec![sym, csym],
                        signatures: vec![sig.clone(), csig.clone()],
                    });
                }
            }
        }
    }
    out
}

// UC4: module/package cycles via strongly connected components.
fn uc4_cycles(g: &GraphHandle<'_>) -> Vec<ReduceFinding> {
    let mut out: Vec<ReduceFinding> = Vec::new();
    let sccs = g.strongly_connected_components();
    for si in 0..sccs.len() {
        let comp = &sccs[si];
        if comp.len() < 2 {
            continue;
        }
        let mut involved: Vec<u64> = Vec::new();
        let mut signatures: Vec<String> = Vec::new();
        for ci in 0..comp.len() {
            let d = comp[ci];
            if let Some(s) = g.resolve_symbol(d) {
                involved.push(s);
                signatures.push(sig_of(g, d).unwrap_or_else(|| String::from("<no-signature>")));
            }
        }
        out.push(ReduceFinding {
            pattern: "uc4_cycle".to_string(),
            message: format!("scc_size={}", comp.len()),
            involved,
            signatures,
        });
    }
    out
}

// Reachability findings must SHIP THE PATH (directional asymmetry rule).
fn path_finding(g: &GraphHandle<'_>, pattern: &str, path: &Vec<u32>) -> ReduceFinding {
    let mut involved: Vec<u64> = Vec::new();
    let mut signatures: Vec<String> = Vec::new();
    for pi in 0..path.len() {
        let d = path[pi];
        if let Some(s) = g.resolve_symbol(d) {
            involved.push(s);
            signatures.push(sig_of(g, d).unwrap_or_else(|| String::from("<no-signature>")));
        }
    }
    ReduceFinding {
        pattern: pattern.to_string(),
        message: format!("path_len={}", path.len()),
        involved,
        signatures,
    }
}

// UC5 probe discovery: (sinks, endpoints, controls).
fn uc5_probes(g: &GraphHandle<'_>, ids: &Vec<u32>) -> (Vec<u32>, Vec<u32>, Vec<u32>) {
    let mut sinks: Vec<u32> = Vec::new();
    let mut endpoints: Vec<u32> = Vec::new();
    let mut controls: Vec<u32> = Vec::new();
    for idx in 0..ids.len() {
        let d = ids[idx];
        let sig = match sig_of(g, d) {
            Some(s) => s,
            None => continue,
        };
        if sig.contains("rawDelete") {
            sinks.push(d);
        }
        if sig.contains("deleteUser") {
            endpoints.push(d);
        }
        if sig.contains("ping") {
            controls.push(d);
        }
    }
    (sinks, endpoints, controls)
}

// UC5: endpoint -> dangerous sink reachability, plus its negative control.
fn uc5_reachability(g: &GraphHandle<'_>, ids: &Vec<u32>) -> Vec<ReduceFinding> {
    // Max call-graph hops explored when asking "can this endpoint reach a sink".
    const MAX_REACHABILITY_DEPTH: usize = 20;

    let mut out: Vec<ReduceFinding> = Vec::new();
    let (sinks, endpoints, controls) = uc5_probes(g, ids);

    out.push(ReduceFinding {
        pattern: "uc5_probe_counts".to_string(),
        message: format!(
            "sinks={} endpoints={} controls={}",
            sinks.len(),
            endpoints.len(),
            controls.len()
        ),
        involved: Vec::new(),
        signatures: Vec::new(),
    });

    for ei in 0..endpoints.len() {
        if let Some(path) = g.shortest_path_to_any(endpoints[ei], &sinks, MAX_REACHABILITY_DEPTH) {
            out.push(path_finding(g, "uc5_endpoint_reaches_sink", &path));
        }
    }

    // Negative control: the health endpoint must reach NO sink.
    // The affirmative marker proves the control was really evaluated;
    // UNEXPECTED_PATH is the failure signal and must never appear.
    for ci in 0..controls.len() {
        let p = controls[ci];
        let sym = match g.resolve_symbol(p) {
            Some(s) => s,
            None => continue,
        };
        let sig = sig_of(g, p).unwrap_or_else(|| String::from("<no-signature>"));
        match g.shortest_path_to_any(p, &sinks, MAX_REACHABILITY_DEPTH) {
            Some(path) => out.push(path_finding(g, "uc5_control_UNEXPECTED_PATH", &path)),
            None => out.push(flag("uc5_control_evaluated", sig.clone(), sym, sig.clone())),
        }
    }
    out
}

fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {
    let mut result = GraphResult::default();
    let ids = enumerate_ids(g);

    result.findings.push(ReduceFinding {
        pattern: "uc0_graph_size".to_string(),
        message: format!("symbols={}", ids.len()),
        involved: Vec::new(),
        signatures: Vec::new(),
    });
    let mut p1 = uc1_uc2_uc6(g, &ids);
    result.findings.append(&mut p1);
    let mut p2 = uc3_layering(g, &ids);
    result.findings.append(&mut p2);
    let mut p3 = uc4_cycles(g);
    result.findings.append(&mut p3);
    let mut p4 = uc5_reachability(g, &ids);
    result.findings.append(&mut p4);

    result
}
"#
    }

    // Story #1854 Step 1: legacy (single-file) template gate. Templates are
    // real `.rs` files under `docs/xray-templates/`, loaded by `include_str!`
    // so a renamed/deleted template is a BUILD ERROR, never a silently empty
    // test set. The cookbook doc copy is checked byte-for-byte against the
    // `.rs` file via the anchor extractor below -- it never compiles the
    // extracted text, it only proves the two copies have not drifted.

    const LEGACY_TEMPLATES: &[&str] =
        &["find-function-definitions", "find-calls-containing-text", "find-node-kind"];

    fn template_source(name: &str) -> &'static str {
        match name {
            "find-function-definitions" => {
                include_str!("../../../docs/xray-templates/find-function-definitions.rs")
            }
            "find-calls-containing-text" => {
                include_str!("../../../docs/xray-templates/find-calls-containing-text.rs")
            }
            "find-node-kind" => include_str!("../../../docs/xray-templates/find-node-kind.rs"),
            "find-definitely-dead-symbols" => {
                include_str!("../../../docs/xray-templates/find-definitely-dead-symbols.rs")
            }
            "find-reference-cycles" => {
                include_str!("../../../docs/xray-templates/find-reference-cycles.rs")
            }
            "report-reachable-symbols-from-dense-id" => include_str!(
                "../../../docs/xray-templates/report-reachable-symbols-from-dense-id.rs"
            ),
            "find-path-to-dense-sink" => {
                include_str!("../../../docs/xray-templates/find-path-to-dense-sink.rs")
            }
            "callers-of-symbols-matching-signature-text" => include_str!(
                "../../../docs/xray-templates/callers-of-symbols-matching-signature-text.rs"
            ),
            other => panic!("template_source: unknown template '{}'", other),
        }
    }

    const GRAPH_TEMPLATES: &[&str] = &[
        "find-definitely-dead-symbols",
        "find-reference-cycles",
        "report-reachable-symbols-from-dense-id",
        "find-path-to-dense-sink",
        "callers-of-symbols-matching-signature-text",
    ];

    fn cookbook_source() -> &'static str {
        include_str!("../../../docs/xray-cookbook.md")
    }

    /// Extracts the byte content of the `rust` fence that immediately
    /// follows `<!-- template:NAME -->` (at most one newline between anchor
    /// and fence). Fails loudly on a missing anchor, a duplicated anchor, a
    /// fence that isn't immediately adjacent, or a missing closing fence --
    /// silently accepting a later, unrelated block would defeat the point of
    /// this check.
    fn extract_cookbook_template_copy(cookbook: &str, name: &str) -> String {
        let anchor = format!("<!-- template:{} -->", name);
        let first = cookbook
            .find(&anchor)
            .unwrap_or_else(|| panic!("cookbook is missing the anchor for template '{}'", name));
        let rest_after_first = &cookbook[first + anchor.len()..];
        assert!(
            !rest_after_first.contains(&anchor),
            "cookbook has a DUPLICATE anchor for template '{}' -- exactly one is required",
            name
        );
        let after_newline = rest_after_first.strip_prefix('\n').unwrap_or(rest_after_first);
        let body = after_newline.strip_prefix("```rust\n").unwrap_or_else(|| {
            panic!("template '{}' anchor must be followed immediately by a ```rust fence", name)
        });
        let fence_close = body
            .find("```")
            .unwrap_or_else(|| panic!("no closing fence found for template '{}'", name));
        body[..fence_close].to_string()
    }

    /// Extracts a template's cookbook PROSE section, bounded between its
    /// own `<!-- template:NAME -->` anchor and the NEXT `<!-- template:`
    /// anchor (or the next `## ` heading, or end of file if neither
    /// exists) -- never end-of-file unconditionally (Story #1854 F5). An
    /// unbounded slice let a needle satisfied by a LATER template's
    /// paragraph pass silently even when the CURRENT template's own
    /// paragraph was deleted entirely -- see
    /// `cookbook_prose_gate_proves_a_deleted_paragraph_is_detected` for
    /// the regression this guards. The fenced ```rust code block
    /// immediately following the anchor is stripped from the returned
    /// text, so a needle appearing only inside example code cannot
    /// satisfy a prose requirement.
    fn extract_cookbook_prose_section(cookbook: &str, name: &str) -> String {
        let anchor = format!("<!-- template:{} -->", name);
        let start = cookbook
            .find(&anchor)
            .unwrap_or_else(|| panic!("cookbook is missing the anchor for template '{}'", name));
        let rest = &cookbook[start + anchor.len()..];
        let next_anchor_offset = rest.find("<!-- template:");
        let next_heading_offset = rest.find("\n## ");
        let end_offset = match (next_anchor_offset, next_heading_offset) {
            (Some(a), Some(h)) => a.min(h),
            (Some(a), None) => a,
            (None, Some(h)) => h,
            (None, None) => rest.len(),
        };
        let section = &rest[..end_offset];
        match (section.find("```"), section.rfind("```")) {
            (Some(fence_start), Some(fence_end)) if fence_end > fence_start => {
                let mut prose = String::new();
                prose.push_str(&section[..fence_start]);
                prose.push_str(&section[fence_end + 3..]);
                prose
            }
            _ => section.to_string(),
        }
    }

    #[test]
    fn legacy_template_cookbook_copies_match_their_rs_files_byte_for_byte() {
        for name in LEGACY_TEMPLATES {
            let doc_copy = extract_cookbook_template_copy(cookbook_source(), name);
            assert_eq!(
                doc_copy,
                template_source(name),
                "docs/xray-cookbook.md's copy of '{}' has drifted from docs/xray-templates/{}.rs",
                name,
                name
            );
        }
    }

    #[test]
    fn graph_template_cookbook_copies_match_their_rs_files_byte_for_byte() {
        for name in GRAPH_TEMPLATES {
            let doc_copy = extract_cookbook_template_copy(cookbook_source(), name);
            assert_eq!(
                doc_copy,
                template_source(name),
                "docs/xray-cookbook.md's copy of '{}' has drifted from docs/xray-templates/{}.rs",
                name,
                name
            );
        }
    }

    #[test]
    fn legacy_templates_classify_as_legacy_mode_and_compile_through_compile_evaluator() {
        use crate::compiler::{self, EvaluatorMode};
        use tempfile::TempDir;

        for name in LEGACY_TEMPLATES {
            let source = template_source(name);
            assert_eq!(
                compiler::detect_evaluator_mode(source)
                    .unwrap_or_else(|e| panic!("template '{}' failed mode detection: {:?}", name, e)),
                EvaluatorMode::Legacy,
                "template '{}' must classify as legacy (single-file) mode",
                name
            );

            let dir = TempDir::new().unwrap();
            let result = compiler::compile_evaluator(source, dir.path());
            assert!(
                result.is_ok(),
                "template '{}' must compile through the real compile_evaluator: {:?}",
                name,
                result.err()
            );
        }
    }

    #[test]
    fn graph_templates_classify_compile_and_export_both_graph_callbacks() {
        use crate::compiler::{self, EvaluatorMode};
        use tempfile::TempDir;

        for name in GRAPH_TEMPLATES {
            let source = template_source(name);
            assert_eq!(
                compiler::detect_evaluator_mode(source)
                    .unwrap_or_else(|e| panic!("template '{}' failed mode detection: {:?}", name, e)),
                EvaluatorMode::Graph,
                "template '{}' must classify as graph mode",
                name
            );
            assert!(source.contains("fn collect_facts"), "{} must define collect_facts", name);
            assert!(source.contains("fn analyze_graph"), "{} must define analyze_graph", name);
            assert!(!source.contains("fn evaluate_node"), "{} must not define evaluate_node", name);
            let dir = TempDir::new().unwrap();
            let evaluator = compile_and_load_graph(source, dir.path());
            assert!(evaluator.has_collect_facts(), "{} must export collect_facts", name);
            assert!(evaluator.has_analyze_graph(), "{} must export analyze_graph", name);
        }
    }

    // F8: the per-symbol `dead_code_not_definite` negative was deliberately
    // removed; the census counter is a strictly stronger control than a
    // pattern-presence check.
    fn assert_dead_code_controls(findings: &[ReduceFinding]) {
        let census = findings
            .iter()
            .find(|f| f.pattern == "dead_code_scan_census")
            .expect("must emit dead_code_scan_census");
        assert_eq!(parse_template_counter(&census.message, "definitely_dead"), 1);
        assert_eq!(parse_template_counter(&census.message, "undecidable"), 5);
        assert_eq!(parse_template_counter(&census.message, "referenced"), 4);
        assert_eq!(parse_template_counter(&census.message, "unresolved"), 0);
        assert!(
            findings.iter().any(|f| f.pattern == "definitely_dead_symbol"),
            "missing true positive definitely_dead_symbol: {:?}", findings
        );
    }

    fn assert_reference_cycle_controls(findings: &[ReduceFinding]) {
        let control = findings
            .iter()
            .find(|f| f.pattern == "reference_cycle_negative_control")
            .expect("must emit reference_cycle_negative_control");
        assert_eq!(parse_template_counter(&control.message, "acyclic_singletons_suppressed"), 8);
        assert_eq!(parse_template_counter(&control.message, "self_loop_singletons"), 0);
        let cycle = findings
            .iter()
            .find(|f| f.pattern == "possible_candidate_cycle")
            .expect("must report the true 2-node cycle");
        assert_eq!(parse_template_counter(&cycle.message, "component_size"), 2);
        assert_eq!(parse_template_counter(&cycle.message, "unresolved_drop_count"), 0);
    }

    fn assert_reachable_controls(findings: &[ReduceFinding]) {
        assert!(
            findings.iter().any(|f| f.pattern == "reachable_root_out_of_range"),
            "missing the constructed out-of-range root finding: {:?}", findings
        );
        let find_root = |root: usize| {
            findings
                .iter()
                .find(|f| f.pattern == "reachable_symbols" && parse_template_counter(&f.message, "root_dense_id") == root)
                .unwrap_or_else(|| panic!("missing reachable_symbols for root {}: {:?}", root, findings))
        };
        assert_eq!(parse_template_counter(&find_root(0).message, "reached_total"), 2);
        assert_eq!(parse_template_counter(&find_root(4).message, "reached_total"), 1);
        assert!(
            findings.iter().any(|f| f.pattern == "reachable_negative_control"),
            "missing negative/control reachable_negative_control: {:?}", findings
        );
    }

    #[test]
    fn graph_templates_execute_with_positive_and_negative_controls() {
        use tempfile::TempDir;

        let graph = six_use_case_test_graph();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);
        fn assert_path_to_sink_controls(findings: &[ReduceFinding]) {
            let census = findings
                .iter()
                .find(|f| f.pattern == "dense_sink_path_census")
                .expect("must emit dense_sink_path_census");
            assert_eq!(parse_template_counter(&census.message, "sources_scanned"), 9);
            assert_eq!(parse_template_counter(&census.message, "paths_found"), 1);
            assert_eq!(parse_template_counter(&census.message, "no_path"), 8);
        }

        fn assert_caller_signature_controls(findings: &[ReduceFinding]) {
            let census = findings
                .iter()
                .find(|f| f.pattern == "signature_match_census")
                .expect("must emit signature_match_census");
            assert_eq!(parse_template_counter(&census.message, "matched"), 1);
            assert_eq!(parse_template_counter(&census.message, "callers"), 1);
            assert!(
                findings.iter().any(|f| f.pattern == "caller_of_signature_match"),
                "missing true positive caller_of_signature_match: {:?}", findings
            );
        }

        let names = [
            "find-definitely-dead-symbols",
            "find-reference-cycles",
            "report-reachable-symbols-from-dense-id",
            "find-path-to-dense-sink",
            "callers-of-symbols-matching-signature-text",
        ];
        for name in names {
            let dir = TempDir::new().unwrap();
            let evaluator = compile_and_load_graph(template_source(name), dir.path());
            let result = evaluator
                .call_analyze_graph(&graph_handle, &facts_handle)
                .expect("graph callback must be exported")
                .expect("template must execute without panic");
            match name {
                "find-definitely-dead-symbols" => assert_dead_code_controls(&result.findings),
                "find-reference-cycles" => assert_reference_cycle_controls(&result.findings),
                "report-reachable-symbols-from-dense-id" => assert_reachable_controls(&result.findings),
                "find-path-to-dense-sink" => assert_path_to_sink_controls(&result.findings),
                "callers-of-symbols-matching-signature-text" => assert_caller_signature_controls(&result.findings),
                _ => unreachable!("unexpected template name {}", name),
            }
        }
    }

    fn parse_template_counter(message: &str, key: &str) -> usize {
        let needle = format!("{}=", key);
        let token = message
            .split_whitespace()
            .find(|token| token.starts_with(&needle))
            .unwrap_or_else(|| panic!("missing {} in {:?}", key, message));
        token[needle.len()..]
            .parse()
            .unwrap_or_else(|_| panic!("non-numeric {} in {:?}", key, message))
    }

    #[test]
    fn graph_template_honesty_controls_are_in_the_inline_prefix() {
        use tempfile::TempDir;

        let graph = six_use_case_test_graph();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);
        let controls = [
            ("find-definitely-dead-symbols", "dead_code_scan_census"),
            ("find-reference-cycles", "reference_cycle_negative_control"),
            ("report-reachable-symbols-from-dense-id", "reachable_root_out_of_range"),
        ];
        for (name, control) in controls {
            let dir = TempDir::new().unwrap();
            let evaluator = compile_and_load_graph(template_source(name), dir.path());
            let result = evaluator
                .call_analyze_graph(&graph_handle, &facts_handle)
                .unwrap()
                .unwrap();
            let index = result
                .findings
                .iter()
                .position(|finding| finding.pattern == control)
                .unwrap_or_else(|| panic!("{} did not emit {}", name, control));
            assert!(index < 3, "{} control {} was emitted at {}", name, control, index);
        }
    }

    #[test]
    fn path_template_reports_real_census_numbers() {
        use tempfile::TempDir;

        let graph = six_use_case_test_graph();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);
        let dir = TempDir::new().unwrap();
        let evaluator = compile_and_load_graph(template_source("find-path-to-dense-sink"), dir.path());
        let result = evaluator.call_analyze_graph(&graph_handle, &facts_handle).unwrap().unwrap();
        let census = result
            .findings
            .iter()
            .find(|finding| finding.pattern == "dense_sink_path_census")
            .expect("path template must emit a census");
        assert_eq!(parse_template_counter(&census.message, "sources_scanned"), 9);
        assert_eq!(parse_template_counter(&census.message, "paths_found"), 1);
        assert_eq!(parse_template_counter(&census.message, "no_path"), 8);
        assert_eq!(result.findings[0].pattern, "dense_sink_path_census");
    }

    #[test]
    fn caller_template_reports_real_census_numbers() {
        use tempfile::TempDir;

        let graph = six_use_case_test_graph();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);
        let dir = TempDir::new().unwrap();
        let evaluator = compile_and_load_graph(
            template_source("callers-of-symbols-matching-signature-text"),
            dir.path(),
        );
        let result = evaluator.call_analyze_graph(&graph_handle, &facts_handle).unwrap().unwrap();
        let census = result
            .findings
            .iter()
            .find(|finding| finding.pattern == "signature_match_census")
            .expect("caller template must emit a census");
        assert_eq!(result.findings[0].pattern, "signature_match_census");
        assert_eq!(parse_template_counter(&census.message, "scanned"), 10);
        assert_eq!(parse_template_counter(&census.message, "matched"), 1);
        assert_eq!(parse_template_counter(&census.message, "missing_signature"), 0);
        assert_eq!(parse_template_counter(&census.message, "matched_with_zero_callers"), 0);
        assert_eq!(parse_template_counter(&census.message, "unresolved_targets"), 0);
        assert_eq!(parse_template_counter(&census.message, "unresolved_callers"), 0);
        assert_eq!(parse_template_counter(&census.message, "callers"), 1);
    }

    #[test]
    fn reference_cycle_template_counts_self_loop_singletons() {
        let source = template_source("find-reference-cycles");
        assert!(source.contains("self_loop_singletons"));
        assert!(source.contains("callees_of(*dense_id).contains(dense_id)"));
        assert!(!source.contains("acyclic singleton components were evaluated and suppressed"));
        assert!(source.contains("possible_candidate_cycle"));
    }

    #[test]
    fn dead_code_template_does_not_emit_per_symbol_negative_noise() {
        let source = template_source("find-definitely-dead-symbols");
        assert!(!source.contains("dead_code_not_definite"));
        assert!(source.contains("referenced="));
        assert!(source.contains("pre-cap referenced bit"));
        assert!(source.contains("reflection"));
    }

    #[test]
    fn reachable_and_path_templates_use_graph_bounded_depth_and_generic_control() {
        let reachable = template_source("report-reachable-symbols-from-dense-id");
        assert!(reachable.contains("let out_of_range_root: u32 = g.symbol_count() as u32"));
        assert!(reachable.contains("reached.len() == 1"));
        assert!(!reachable.contains("root == 4 && reached.len() == 1"));
        assert!(reachable.contains("g.symbol_count()"));

        let paths = template_source("find-path-to-dense-sink");
        assert!(!paths.contains("NO_PATH_SOURCE"));
        assert!(paths.contains("g.symbol_count()"));
    }

    #[test]
    fn cookbook_prose_gate_must_not_search_past_the_current_template() {
        let cookbook = cookbook_source();
        let cycles = "<!-- template:find-reference-cycles -->";
        let next = "<!-- template:report-reachable-symbols-from-dense-id -->";
        let start = cookbook.find(cycles).unwrap();
        let end = cookbook[start..]
            .find(next)
            .map(|offset| start + offset)
            .unwrap();
        let section = &cookbook[start..end];
        assert!(section.contains("unresolved"));
        assert!(!section.contains("out-of-range"));
        let gate_source = include_str!("dynlib.rs");
        // Built from two literals, never one contiguous string: a single
        // literal here would make this file contain its own needle (via
        // this very assertion), so the check could never pass.
        let vulnerable_pattern = format!("{}{}", "let section = &cookbook[start", "..];");
        assert!(
            !gate_source.contains(&vulnerable_pattern),
            "the prose gate must bound each template section"
        );
    }

    #[test]
    fn template_inventory_cross_check_covers_every_rs_file() {
        use std::fs;
        let directory = fs::read_dir("../../docs/xray-templates")
            .expect("template directory must be readable");
        for entry in directory {
            let entry = entry.unwrap();
            let path = entry.path();
            if path.extension().and_then(|extension| extension.to_str()) != Some("rs") {
                continue;
            }
            let name = path.file_stem().unwrap().to_str().unwrap();
            assert!(
                LEGACY_TEMPLATES.contains(&name) || GRAPH_TEMPLATES.contains(&name),
                "template {} is not listed in a mode inventory",
                name
            );
        }
    }

    #[test]
    fn gate_negative_control_invalid_syntax_fails() {
        use crate::compiler::compile_evaluator;
        use tempfile::TempDir;
        let result = compile_evaluator("fn evaluate_node( {", TempDir::new().unwrap().path());
        assert!(result.is_err());
    }

    #[test]
    fn gate_negative_control_unavailable_type_or_method_fails() {
        use crate::compiler::compile_evaluator;
        use tempfile::TempDir;
        let source = "fn evaluate_node(node: &XRayNode<'_>, file: &str) -> Vec<ReduceFinding> { vec![ReduceFinding { pattern: node.child_by_field_name(\"x\"), message: file.to_string(), involved: Vec::new(), signatures: Vec::new() }] }";
        assert!(compile_evaluator(source, TempDir::new().unwrap().path()).is_err());
    }

    #[test]
    fn gate_negative_control_mixed_mode_fails() {
        use crate::compiler::detect_evaluator_mode;
        let source = "fn evaluate_node(node: &XRayNode<'_>, file: &str) -> Vec<ReduceFinding> { Vec::new() } fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() } fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }";
        assert!(detect_evaluator_mode(source).is_err());
    }

    #[test]
    fn gate_negative_control_missing_graph_callback_fails() {
        use crate::compiler::detect_evaluator_mode;
        let source = "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }";
        assert!(detect_evaluator_mode(source).is_err());
    }

    #[test]
    fn gate_negative_control_deleted_template_name_fails() {
        let result = std::panic::catch_unwind(|| template_source("deleted-template"));
        assert!(result.is_err());
    }

    #[test]
    fn gate_negative_control_drifted_doc_copy_fails() {
        let mut cookbook = cookbook_source().to_string();
        cookbook = cookbook.replacen(
            template_source("find-function-definitions"),
            "fn drifted_collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }",
            1,
        );
        let result = std::panic::catch_unwind(|| {
            assert_eq!(
                extract_cookbook_template_copy(&cookbook, "find-function-definitions"),
                template_source("find-function-definitions")
            );
        });
        assert!(result.is_err());
    }

    /// Standalone RED/GREEN discriminator for the find-definitely-dead-symbols
    /// census counter (Story #1854 pair-fix). The loop test above only checks
    /// pattern PRESENCE and passes identically before and after this fix; this
    /// test asserts on the actual census message content so a mislabeled
    /// counter is caught. Reuses `six_use_case_test_graph()` UNCHANGED: of its
    /// 10 interned symbols, `never_called` (private, unreferenced) is the one
    /// `Some(true)` (definitely dead); `order_repository`, `raw_delete_row`,
    /// `cycle_a`, `cycle_b` (all `mark_referenced`) are `Some(false)`
    /// (referenced -- 4 of them); the remaining 5 are unreferenced with
    /// non-private visibility, i.e. `None` (undecidable).
    #[test]
    fn find_definitely_dead_symbols_census_counters_are_independent() {
        use tempfile::TempDir;

        /// Parses `key=N` out of the real census message. Panics (never
        /// defaults to 0) when the key is absent or its value is not a
        /// valid `usize`, so a census that stops carrying real numbers
        /// fails loudly instead of passing on a silently-defaulted count.
        fn parse_census_counter(message: &str, key: &str) -> usize {
            let needle = format!("{}=", key);
            let value_start = message
                .find(&needle)
                .unwrap_or_else(|| panic!("census message missing key {:?}: {}", key, message))
                + needle.len();
            let value_end = message[value_start..]
                .find(|c: char| !c.is_ascii_digit())
                .map(|offset| value_start + offset)
                .unwrap_or(message.len());
            let digits = &message[value_start..value_end];
            digits.parse::<usize>().unwrap_or_else(|e| {
                panic!("census key {:?} has unparseable value {:?}: {}", key, digits, e)
            })
        }

        let graph = six_use_case_test_graph();
        let facts = crate::graph::user_facts::FactIndex::new();
        let graph_handle = crate::graph::csr::handle::GraphHandle::from_graph(&graph);
        let facts_handle = crate::graph::user_facts::FactsHandle::from_facts(&facts);

        let dir = TempDir::new().unwrap();
        let evaluator =
            compile_and_load_graph(template_source("find-definitely-dead-symbols"), dir.path());
        let result = evaluator
            .call_analyze_graph(&graph_handle, &facts_handle)
            .expect("graph callback must be exported")
            .expect("template must execute without panic");

        let census = result
            .findings
            .iter()
            .find(|f| f.pattern == "dead_code_scan_census")
            .expect("template must emit a dead_code_scan_census finding");

        let definitely_dead = parse_census_counter(&census.message, "definitely_dead");
        let undecidable = parse_census_counter(&census.message, "undecidable");
        let referenced = parse_census_counter(&census.message, "referenced");
        let unresolved = parse_census_counter(&census.message, "unresolved");

        assert_eq!(
            definitely_dead, 1,
            "census must report exactly 1 definitely-dead symbol (never_called): {}",
            census.message
        );
        assert_eq!(
            undecidable, 5,
            "census must report exactly 5 undecidable symbols: {}",
            census.message
        );
        assert_eq!(
            referenced, 4,
            "census must count the 4 referenced (Some(false)) symbols under `referenced`, \
             not duplicate the undecidable count: {}",
            census.message
        );
        assert_eq!(
            unresolved, 0,
            "every interned symbol in this fixture must resolve: {}",
            census.message
        );
        assert_ne!(
            referenced, undecidable,
            "referenced and undecidable must be independently derived counters, not duplicates"
        );

        assert_eq!(
            definitely_dead + undecidable + referenced + unresolved,
            graph.symbol_count(),
            "the four parsed census counters must account for every dense id scanned"
        );
    }

    #[test]
    fn graph_template_cookbook_prose_contains_required_honesty_tripwires() {
        let cookbook = cookbook_source();
        let required: &[(&str, &[&str])] = &[
            ("find-definitely-dead-symbols", &["is_definitely_dead_code", "None", "not a completeness signal", "text matching", "not name resolution"]),
            ("find-reference-cycles", &["fact_graph_complete", "caller-supplied dense IDs", "is_definitely_dead_code", "not a completeness signal", "text matching", "not name resolution", "unresolved"]),
            ("report-reachable-symbols-from-dense-id", &["fact_graph_complete", "caller-supplied dense IDs", "is_definitely_dead_code", "not a completeness signal", "text matching", "not name resolution", "symbol_count", "out-of-range"]),
            ("find-path-to-dense-sink", &["fact_graph_complete", "caller-supplied dense IDs", "is_definitely_dead_code", "not a completeness signal", "text matching", "not name resolution", "no path"]),
            ("callers-of-symbols-matching-signature-text", &["fact_graph_complete", "caller-supplied dense IDs", "is_definitely_dead_code", "not a completeness signal", "text matching", "not name resolution"]),
        ];
        for (name, needles) in required {
            let section = extract_cookbook_prose_section(cookbook, name);
            for needle in *needles {
                assert!(section.contains(needle), "{} cookbook section must mention {:?}", name, needle);
            }
        }
    }

    /// Builds an interior node whose `text()` is exactly `text` -- derives
    /// `end_byte` from `text.len()` instead of a hand-counted literal, so a
    /// mismatched byte count can never silently truncate `text()` to "" via
    /// `new_node_for_test`'s own out-of-bounds `unwrap_or("")` fallback.
    fn node_with_text(kind: &str, text: &str, line: usize, children: Vec<OwnedNode>) -> OwnedNode {
        OwnedNode::new_node_for_test(kind, text, line, 0, text.len(), children, true)
    }

    /// True positive: `computeTotal` (method_declaration) with calls to
    /// `rawDeleteRow` (contains "rawDelete") and `safeUpdate` (does not).
    /// Negative controls: the `total` field_declaration (never a function
    /// definition) and the `safeUpdate` call (never matches "rawDelete").
    fn method_body_children() -> Vec<OwnedNode> {
        vec![
            OwnedNode::new_leaf_for_test("identifier", "computeTotal", 3, true),
            node_with_text("method_invocation", "rawDeleteRow(id)", 4, vec![]),
            node_with_text("method_invocation", "safeUpdate(id)", 5, vec![]),
        ]
    }

    /// The find-node-kind true positive is the `class_declaration`; its
    /// negative control is the enclosing `program` root, which must not
    /// itself be reported.
    fn legacy_template_fixture_tree() -> OwnedNode {
        let method_decl = node_with_text(
            "method_declaration",
            "void computeTotal() { rawDeleteRow(id); safeUpdate(id); }",
            3,
            method_body_children(),
        );
        let field_decl = OwnedNode::new_leaf_for_test("field_declaration", "int total;", 2, true);
        let class_decl = node_with_text(
            "class_declaration",
            "class Order { int total; void computeTotal() { ... } }",
            1,
            vec![field_decl, method_decl],
        );
        node_with_text("program", "", 1, vec![class_decl])
    }

    fn compile_and_load_legacy(source: &str, dir: &std::path::Path) -> DynlibEvaluator {
        let cr = crate::compiler::compile_evaluator(source, dir)
            .unwrap_or_else(|e| panic!("template must compile: {:?}", e));
        DynlibEvaluator::load(&cr.so_path)
            .unwrap_or_else(|e| panic!("compiled template .so must load: {}", e))
    }

    #[test]
    fn find_function_definitions_template_reports_methods_not_fields() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let evaluator =
            compile_and_load_legacy(template_source("find-function-definitions"), dir.path());
        let findings = evaluator.evaluate_node(&legacy_template_fixture_tree());
        assert!(
            findings.iter().any(|f| f.snippet.contains("computeTotal")),
            "must report the method_declaration as a function definition: {:?}",
            findings
        );
        assert!(
            !findings.iter().any(|f| f.snippet.contains("total;")),
            "must NOT report the field_declaration as a function definition: {:?}",
            findings
        );
    }

    #[test]
    fn find_calls_containing_text_template_matches_sink_not_safe_call() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let evaluator =
            compile_and_load_legacy(template_source("find-calls-containing-text"), dir.path());
        let findings = evaluator.evaluate_node(&legacy_template_fixture_tree());
        assert!(
            findings.iter().any(|f| f.snippet.contains("rawDeleteRow")),
            "must report the call containing the target text: {:?}",
            findings
        );
        assert!(
            !findings.iter().any(|f| f.snippet.contains("safeUpdate")),
            "must NOT report a call that does not contain the target text: {:?}",
            findings
        );
    }

    #[test]
    fn find_node_kind_template_matches_class_not_program() {
        use tempfile::TempDir;
        let dir = TempDir::new().unwrap();
        let evaluator = compile_and_load_legacy(template_source("find-node-kind"), dir.path());
        let findings = evaluator.evaluate_node(&legacy_template_fixture_tree());
        assert!(
            findings
                .iter()
                .any(|f| f.pattern == "node_kind_match" && f.snippet.contains("class Order")),
            "must report the class_declaration node: {:?}",
            findings
        );
        assert_eq!(
            findings.len(),
            1,
            "must not report the enclosing program node or anything else: {:?}",
            findings
        );
    }
}
