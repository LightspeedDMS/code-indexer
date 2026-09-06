use crate::finding::EvalFinding;
use crate::owned_node::OwnedNode;
use crate::scanner::Evaluator;
use libloading::{Library, Symbol};
use std::path::Path;

type EvaluateNodeFn = fn(&OwnedNode) -> Vec<EvalFinding>;
type AbiVersionFn = fn() -> u64;
type DrainDebugLogFn = fn() -> Vec<String>;

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

/// Story #1787 AC8: loads a GRAPH-MODE compiled evaluator, distinct from
/// `DynlibEvaluator` (legacy-only). Mirrors `DynlibEvaluator::load`'s ABI
/// verification exactly, then resolves `xray_collect_facts`/
/// `xray_analyze_graph` the SAME optional-symbol way
/// `xray_drain_debug_log` already is -- a missing symbol is a legitimate
/// outcome (a legacy-mode `.so` loaded here has neither), reported via
/// `has_collect_facts`/`has_analyze_graph`, never a load failure.
pub struct GraphDynlibEvaluator {
    _lib: Library,
    collect_facts_fn: Option<CollectFactsFn>,
    analyze_graph_fn: Option<AnalyzeGraphFn>,
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

        let collect_facts_fn: Option<CollectFactsFn> =
            unsafe { lib.get::<CollectFactsFn>(b"xray_collect_facts").ok().map(|sym| *sym) };
        let analyze_graph_fn: Option<AnalyzeGraphFn> =
            unsafe { lib.get::<AnalyzeGraphFn>(b"xray_analyze_graph").ok().map(|sym| *sym) };

        Ok(Self { _lib: lib, collect_facts_fn, analyze_graph_fn })
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
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
