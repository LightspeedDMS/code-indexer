use crate::finding::EvalFinding;
use crate::owned_node::OwnedNode;
use crate::scanner::Evaluator;
use libloading::{Library, Symbol};
use std::path::Path;

type EvaluateNodeFn = fn(&OwnedNode) -> Vec<EvalFinding>;
type AbiVersionFn = fn() -> u64;
type DrainDebugLogFn = fn() -> Vec<String>;

/// Must match `XRAY_ABI_VERSION` in compiler.rs PREAMBLE.
/// Increment both when OwnedNode or EvalFinding layout changes.
const EXPECTED_ABI_VERSION: u64 = 2;

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
        if abi_version != EXPECTED_ABI_VERSION {
            return Err(format!(
                "ABI version mismatch: evaluator has version {} but loader expects {}. \
                 Recompile your evaluator.",
                abi_version, EXPECTED_ABI_VERSION
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

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

        // Load should succeed only when abi version matches EXPECTED_ABI_VERSION
        let evaluator = DynlibEvaluator::load(&cr.so_path);
        assert!(evaluator.is_ok(), "load must succeed with matching ABI version: {:?}", evaluator.err());
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

        let source: Arc<str> = Arc::from("test");
        let node = OwnedNode {
            kind: "test_node".to_string(),
            start_line: 42,
            start_byte: 0,
            end_byte: 4,
            children: vec![],
            is_named: true,
            source,
        };
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

        let source: Arc<str> = Arc::from("x");
        let node = OwnedNode {
            kind: "some_node".to_string(),
            start_line: 1,
            start_byte: 0,
            end_byte: 1,
            children: vec![],
            is_named: true,
            source,
        };
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

        let source: Arc<str> = Arc::from("");
        let node = OwnedNode {
            kind: "root".to_string(),
            start_line: 1,
            start_byte: 0,
            end_byte: 0,
            children: vec![],
            is_named: true,
            source,
        };
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

        let source: Arc<str> = Arc::from("");
        let node = OwnedNode {
            kind: "root".to_string(),
            start_line: 1,
            start_byte: 0,
            end_byte: 0,
            children: vec![],
            is_named: true,
            source,
        };

        // Call through trait reference — this is how scanner.rs uses evaluators.
        let eval_ref: &dyn Evaluator = &evaluator;
        eval_ref.evaluate_node(&node);
        let messages = eval_ref.drain_debug_log();
        assert_eq!(messages.len(), 1, "trait dispatch must return debug messages: {:?}", messages);
        assert_eq!(messages[0], "trait dispatch test");
    }
}
